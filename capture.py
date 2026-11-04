"""Open the embed page in a real browser, record EVERY request and response it makes, and find the stream in that.

Why: many players build the stream address in JavaScript (API call -> token -> playlist), so it is in no HTML and no
script. A browser runs the page like a viewer would; whatever the player asks the network for is recorded here:

  record()   drives Chromium (Playwright): loads the page, presses play, keeps every request, response, header,
             cookie and the text of every API / script / playlist response   -> Capture
  analyze()  looks through the Capture for streams: playlists the player requested, playlists in response bodies,
             URLs inside API JSON, video files - ranks them (long VOD master > preview / ad / live)  -> [Stream]
  extract()  record + analyze + a real check that each candidate downloads with the headers the browser used
             -> (stream url, headers) for the downloader; used by m3u8.extract_m3u8 and /capture

The Capture also exports as a zip (requests table, HAR for browser dev-tools, response bodies, stream ranking).
Needs:  pip install playwright && playwright install chromium   (CAPTURE_MODE=fallback|first, see config.py)"""
import asyncio
import html
import io
import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import config
import m3u8

log = logging.getLogger("capture")

# ---------------------------------------------------------------- what is what
_AD = m3u8.AD_RE
_SEGMENT = re.compile(r"\.(?:ts|m4s|aac|m4a|cmfv|cmfa|vtt|srt|webvtt|key)(?:[?#]|$)|(?:seg|chunk|frag)(?:ment)?[-_]?\d+", re.I)
_HLS_URL = re.compile(r"\.m3u8(?:[?#]|$)", re.I)
_FILE_URL = re.compile(r"\.(?:mp4|mkv|webm|mov|m4v)(?:[?#]|$)", re.I)
_TEXTISH = re.compile(r"json|javascript|ecmascript|text/|xml|mpegurl|x-www-form", re.I)
_MIN_MOVIE_SECONDS = 120   # a playlist shorter than this is a preview, trailer or ad
_AD_BODY_BYTES = 2_000_000
STEALTH = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); window.chrome = window.chrome || {runtime: {}};"
_SENSITIVE = {"cookie", "set-cookie", "authorization", "x-api-key", "proxy-authorization"}
_LOCK = asyncio.Lock()   # one browser at a time: Chromium needs a few hundred MB


def available() -> bool:
    try:
        import playwright.async_api  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------- the recording
@dataclass
class Entry:
    n: int
    method: str
    url: str
    rtype: str = ""
    req_headers: dict = field(default_factory=dict)
    post: str = ""
    frame: str = ""
    status: int = 0
    resp_headers: dict = field(default_factory=dict)
    ctype: str = ""
    size: int = 0
    body: str | None = None
    failed: str = ""          # "blocked" for requests we aborted, else the browser's failure text
    t0: float = 0.0
    t1: float = 0.0


class Capture:
    def __init__(self, page_url: str):
        self.page_url = page_url
        self.entries: list[Entry] = []
        self.cookies: list[dict] = []
        self.notes: list[str] = []
        self.started = time.time()
        self.finished = 0.0
        self.body_budget = config.CAPTURE_BODY_MB * 1024 * 1024
        self._by_req: dict[int, Entry] = {}
        self.blocked_ids: set[int] = set()   # requests we aborted (the 'request' event may arrive before or after the route)
        self.tasks: list[asyncio.Task] = []

    # -- filled by the browser events
    def on_request(self, req) -> Entry:
        try:
            frame = req.frame.url
        except Exception:  # noqa: BLE001  (service worker / detached frame)
            frame = ""
        try:
            post = (req.post_data or "")[:2000]
        except Exception:  # noqa: BLE001  (binary body)
            post = "<binary>"
        e = Entry(len(self.entries) + 1, req.method, req.url, req.resource_type, {k.lower(): v for k, v in req.headers.items()},
                  post, frame, t0=time.time())
        if id(req) in self.blocked_ids:
            e.failed, e.t1 = "blocked", time.time()
        self.entries.append(e)
        self._by_req[id(req)] = e
        return e

    def entry_for(self, req) -> Entry | None:
        return self._by_req.get(id(req))

    # -- exports
    def counts(self) -> str:
        by: dict[str, int] = {}
        for e in self.entries:
            by[e.rtype or "?"] = by.get(e.rtype or "?", 0) + 1
        return ", ".join(f"{v} {k}" for k, v in sorted(by.items(), key=lambda x: -x[1]))

    def table(self) -> str:
        rows = ["  #  METHOD  STATUS  TYPE        SIZE      MS  URL"]
        for e in self.entries:
            st = "BLOCK" if e.failed == "blocked" else ("FAIL" if e.failed else str(e.status or "-"))
            ms = f"{(e.t1 - e.t0) * 1000:.0f}" if e.t1 else "-"
            rows.append(f"{e.n:3d}  {e.method:6}  {st:6}  {e.rtype[:10]:10}  {e.size:>8}  {ms:>6}  {e.url}" + (f"   !! {e.failed}" if e.failed and e.failed != "blocked" else ""))
        return "\n".join(rows)

    def har(self, scrub=lambda t: t) -> dict:
        def hdrs(d: dict) -> list[dict]:
            return [{"name": k, "value": "‹hidden›" if k.lower() in _SENSITIVE else scrub(str(v))} for k, v in d.items()]
        entries = []
        for e in self.entries:
            entries.append({
                "startedDateTime": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(e.t0)) + f".{int(e.t0 % 1 * 1000):03d}Z",
                "time": round((e.t1 - e.t0) * 1000) if e.t1 else 0,
                "_resourceType": e.rtype,
                "request": {"method": e.method, "url": scrub(e.url), "httpVersion": "HTTP/1.1", "headers": hdrs(e.req_headers),
                            "queryString": [], "cookies": [], "headersSize": -1, "bodySize": len(e.post),
                            **({"postData": {"mimeType": e.req_headers.get("content-type", ""), "text": scrub(e.post)}} if e.post else {})},
                "response": {"status": e.status, "statusText": "", "httpVersion": "HTTP/1.1", "headers": hdrs(e.resp_headers), "cookies": [],
                             "content": {"size": e.size, "mimeType": e.ctype, **({"text": scrub(e.body)} if e.body is not None else {})},
                             "redirectURL": e.resp_headers.get("location", ""), "headersSize": -1, "bodySize": e.size,
                             **({"_error": e.failed} if e.failed else {})},
                "cache": {}, "timings": {"send": 0, "wait": round((e.t1 - e.t0) * 1000) if e.t1 else 0, "receive": 0},
            })
        return {"log": {"version": "1.2", "creator": {"name": "moviebot", "version": "1"}, "pages": [], "entries": entries}}

    def files(self, streams: list["Stream"] | None = None, scrub=lambda t: t) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        streams = streams if streams is not None else analyze(self)
        head = [f"BROWSER CAPTURE of {self.page_url}", f"{len(self.entries)} requests in {self.finished - self.started:.0f}s: {self.counts()}"]
        hosts: dict[str, int] = {}
        for e in self.entries:
            h = urlsplit(e.url).netloc
            hosts[h] = hosts.get(h, 0) + 1
        head += ["hosts: " + ", ".join(f"{h} ({n})" for h, n in sorted(hosts.items(), key=lambda x: -x[1])[:15]), ""]
        head += self.notes + ["", "STREAMS FOUND (best first):", format_streams(streams)]
        out["report.txt"] = scrub("\n".join(head)).encode()
        out["requests.txt"] = scrub(self.table()).encode()
        out["streams.txt"] = scrub(format_streams(streams, full=True)).encode()
        out["capture.har"] = json.dumps(self.har(scrub), ensure_ascii=False, indent=1).encode()
        out["cookies.txt"] = "\n".join(f"{c.get('domain')}\t{c.get('name')}\t(value hidden)" for c in self.cookies).encode()
        for e in self.entries:
            if e.body:
                u = urlsplit(e.url)
                tail = re.sub(r"[^A-Za-z0-9._-]+", "_", u.netloc + u.path)[:60].strip("_")
                out[f"bodies/{e.n:03d}_{tail}.txt"] = scrub(f"{e.method} {e.url}\nHTTP {e.status}  {e.ctype}  {e.size} bytes\n"
                                                           f"from frame: {e.frame}\n\n{e.body}").encode()
        return out

    def zip_bytes(self, streams=None, scrub=lambda t: t) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in self.files(streams, scrub).items():
                z.writestr(name, data)
        return buf.getvalue()


# ---------------------------------------------------------------- the browser
def _is_blocked(url: str, rtype: str) -> bool:
    if config.CAPTURE_BLOCK_ADS and _AD.search(url):
        return True
    return rtype in ("image", "font") and config.CAPTURE_BLOCK_ASSETS


def _wants_body(e: Entry, size: int) -> bool:
    """Keep the text of API answers, scripts, pages and playlists; never media, images or segments."""
    if e.rtype in ("image", "font", "stylesheet", "media", "texttrack"):
        return False
    if _SEGMENT.search(e.url) and not _HLS_URL.search(e.url):
        return False
    if size > _AD_BODY_BYTES:
        return False
    return e.rtype in ("document", "xhr", "fetch", "script", "other", "eventsource") or bool(_TEXTISH.search(e.ctype))


async def _grab(cap: Capture, e: Entry, resp) -> None:
    try:
        e.status = resp.status
        e.resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        e.ctype = e.resp_headers.get("content-type", "")
        try:
            e.size = int(e.resp_headers.get("content-length") or 0)
        except ValueError:
            e.size = 0
        if "content-range" in e.resp_headers:
            m = re.search(r"/(\d+)$", e.resp_headers["content-range"])
            e.size = int(m.group(1)) if m else e.size
        if 300 <= resp.status < 400 or not _wants_body(e, e.size) or cap.body_budget <= 0:
            return
        data = await resp.body()
        e.size = e.size or len(data)
        text = data[:400_000].decode("utf-8", "ignore")
        cap.body_budget -= len(text)
        e.body = text
    except Exception:  # noqa: BLE001  (redirect / closed page: no body to keep)
        pass
    finally:
        e.t1 = e.t1 or time.time()


_PLAY_SELECTORS = "video, .jw-icon-display, .vjs-big-play-button, .plyr__control--overlaid, .play-btn, .play, #player, [class*=play]"


async def _poke(page) -> None:
    """Start playback the way a viewer would: unmute-less autoplay of every <video>, a click on the play button / middle."""
    for fr in list(page.frames):
        try:
            await fr.evaluate("() => document.querySelectorAll('video').forEach(v => { v.muted = true; const p = v.play(); if (p && p.catch) p.catch(() => {}); })")
        except Exception:  # noqa: BLE001
            pass
        try:
            await fr.click(_PLAY_SELECTORS, timeout=600, force=True, no_wait_after=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        await page.mouse.click(500, 320)
    except Exception:  # noqa: BLE001
        pass


async def record(url: str, referer: str = "", seconds: int | None = None, on_status=None) -> Capture:
    """Load `url` in headless Chromium for up to `seconds`, press play, and keep everything. Stops early once a
    long stream was seen (and two more seconds went by, for the other qualities)."""
    from playwright.async_api import async_playwright  # noqa: PLC0415

    cap = Capture(url)
    seconds = seconds or config.CAPTURE_SECONDS
    say = on_status or (lambda s: None)
    async with async_playwright() as pw:
        launch = dict(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
                                           "--autoplay-policy=no-user-gesture-required", "--mute-audio"])
        if config.CAPTURE_CHROMIUM:
            launch["executable_path"] = config.CAPTURE_CHROMIUM
        if config.PROXIES:
            launch["proxy"] = {"server": config.PROXIES[0]}
        browser = await pw.chromium.launch(**launch)
        try:
            ctx = await browser.new_context(user_agent=m3u8.UA, viewport={"width": 1000, "height": 640}, locale="en-US",
                                            ignore_https_errors=True)
            await ctx.add_init_script(STEALTH)

            async def router(route):
                req = route.request
                if _is_blocked(req.url, req.resource_type):
                    cap.blocked_ids.add(id(req))
                    e = cap.entry_for(req)
                    if e:
                        e.failed, e.t1 = "blocked", time.time()
                    await route.abort()
                else:
                    await route.continue_()

            await ctx.route("**/*", router)
            ctx.on("request", lambda req: cap.on_request(req))

            def on_response(resp):
                e = cap.entry_for(resp.request)
                if e:
                    cap.tasks.append(asyncio.ensure_future(_grab(cap, e, resp)))
            ctx.on("response", on_response)

            def on_failed(req):
                e = cap.entry_for(req)
                if e and not e.failed:
                    e.failed, e.t1 = (req.failure or "failed"), time.time()
            ctx.on("requestfailed", on_failed)

            def on_finished(req):
                e = cap.entry_for(req)
                if e:
                    e.t1 = time.time()
            ctx.on("requestfinished", on_finished)

            page = await ctx.new_page()
            page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

            def on_popup(p):  # popups are ads: close them (their requests stay in the recording)
                if p is not page:
                    cap.notes.append(f"popup closed: {p.url or '(opening)'}")
                    asyncio.ensure_future(p.close())
            ctx.on("page", on_popup)

            say("opening the page in a browser…")
            try:
                await page.goto(url, referer=referer or None, wait_until="domcontentloaded", timeout=45_000)
            except Exception as exc:  # noqa: BLE001  (a slow page: whatever loaded is still recorded)
                cap.notes.append(f"page load: {type(exc).__name__}: {str(exc)[:160]}")
            seen_good = 0
            for sec in range(seconds):
                await page.wait_for_timeout(1000)
                if sec in (2, 5, 8, 12, 17, 23, 30):
                    await _poke(page)
                say(f"watching the network… {len(cap.entries)} requests")
                top = analyze(cap)[:1]
                if top and top[0].score >= 90 and top[0].observed and (not top[0].duration or top[0].duration >= _MIN_MOVIE_SECONDS):
                    seen_good += 1
                    if seen_good >= 3:   # found, and stable for two more seconds: the other qualities have been requested
                        cap.notes.append(f"stopped early after {sec + 1}s: a stream was found")
                        break
            if cap.tasks:
                await asyncio.wait(cap.tasks, timeout=8)
            try:
                cap.cookies = await ctx.cookies()
            except Exception:  # noqa: BLE001
                pass
        finally:
            await browser.close()
    cap.finished = time.time()
    return cap


# ---------------------------------------------------------------- finding the streams
@dataclass
class Stream:
    url: str
    kind: str                 # "hls" | "file"
    score: float = 0.0
    observed: bool = False    # the player itself requested it (vs. only mentioned inside some response)
    why: list[str] = field(default_factory=list)
    duration: float = 0.0     # seconds, when the playlist body was captured
    master: bool = False
    heights: list[int] = field(default_factory=list)
    size: int = 0
    headers: dict = field(default_factory=dict)   # what to send to download it
    source: str = ""          # the response it was mentioned in


def parse_playlist(body: str) -> dict:
    master = "#EXT-X-STREAM-INF" in body
    dur = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", body))
    heights = sorted({int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", body)}, reverse=True)
    return {"master": master, "duration": dur, "heights": heights, "vod": "#EXT-X-ENDLIST" in body,
            "segments": body.count("#EXTINF"), "encrypted": "#EXT-X-KEY" in body and "METHOD=NONE" not in body}


def _host_cookies(cap: Capture, host: str) -> str:
    pairs = []
    for c in cap.cookies:
        d = (c.get("domain") or "").lstrip(".").lower()
        if d and (host == d or host.endswith("." + d)):
            pairs.append(f"{c['name']}={c['value']}")
    return "; ".join(pairs)


def replay_headers(cap: Capture, url: str, e: Entry | None) -> dict:
    """The headers the browser used for this request (UA, Referer, Origin) + that site's cookies."""
    h = {"User-Agent": m3u8.UA, "Accept": "*/*"}
    ref = (e.req_headers.get("referer") if e else "") or (e.frame if e else "") or cap.page_url
    h["Referer"] = ref
    org = (e.req_headers.get("origin") if e else "") or ""
    if org:
        h["Origin"] = org
    ck = _host_cookies(cap, (urlsplit(url).hostname or "").lower())
    if ck:
        h["Cookie"] = ck
    return h


def analyze(cap: Capture) -> list[Stream]:
    """Every stream the recording shows, best first. Pure function of the Capture (no network)."""
    found: dict[str, Stream] = {}

    def add(url: str, kind: str, e: Entry, observed: bool, note: str, info: dict | None = None, source: str = "") -> None:
        url = html.unescape(url)
        if not url.startswith("http") or _SEGMENT.search(url) and kind == "hls" and not _HLS_URL.search(url):
            return
        s = found.get(url)
        if s is None:
            s = found[url] = Stream(url, kind, source=source)
        if observed and not s.observed:
            s.observed, s.headers = True, replay_headers(cap, url, e)
        if not s.headers:
            s.headers = replay_headers(cap, url, None if not observed else e)
            if source:  # a URL only mentioned in a response: the browser would have asked from that page
                s.headers["Referer"] = e.frame or e.url
        if info:
            s.master, s.duration, s.heights = info["master"] or s.master, info["duration"] or s.duration, info["heights"] or s.heights
            if info["duration"]:  # a media playlist: say whether it is a finished video or a live stream
                s.why.append("VOD" if info["vod"] else "live / no end marker")
        s.size = max(s.size, e.size if observed else 0)
        if note and note not in s.why:
            s.why.append(note)

    for e in cap.entries:
        if e.failed or (e.status and e.status >= 400):
            continue
        low, ct, body = e.url.lower(), e.ctype.lower(), e.body
        is_pl = bool(body and body.lstrip("\ufeff \r\n\t").startswith("#EXTM3U"))
        if is_pl or (_HLS_URL.search(low) and e.status in (200, 206)) or "mpegurl" in ct:
            info = parse_playlist(body) if is_pl else None
            add(e.url, "hls", e, True, "playlist requested by the player" + (" (found by its content)" if is_pl and not _HLS_URL.search(low) else ""), info)
        elif (e.rtype == "media" or "video/" in ct or _FILE_URL.search(low)) and not _SEGMENT.search(low):
            add(e.url, "file", e, True, f"video file requested by the player ({e.size // 1048576} MB)" if e.size else "video file requested by the player")
        if body and not is_pl and (_TEXTISH.search(ct) or e.rtype in ("document", "xhr", "fetch", "script")):
            src = urlsplit(e.url)
            where = f"mentioned in {src.netloc}{src.path[:40]}"
            for u in m3u8.find_m3u8_urls(body, e.url):
                add(u, "hls", e, False, where, source=e.url)
            for u in m3u8.find_streamish_urls([body], e.url):
                add(u, "hls", e, False, where + " (no .m3u8 in the name)", source=e.url)
            for u in m3u8.find_obfuscated_urls([body]):
                add(u, "hls", e, False, where + " (hidden / encoded)", source=e.url)
            for u in m3u8.find_direct_files([body]):
                add(u, "file", e, False, where, source=e.url)

    for s in found.values():
        score = 50.0 + (25 if s.observed else 0)
        host_path = urlsplit(s.url)
        if _AD.search(s.url):
            score -= 70
            s.why.append("looks like an ad / tracker")
        if s.kind == "hls":
            if s.master:
                score += 20
                s.why.append("master playlist" + (f" ({', '.join(f'{h}p' for h in s.heights[:4])})" if s.heights else ""))
            if re.search(r"master|index|playlist", host_path.path, re.I):
                score += 5
            if s.duration:
                score += min(s.duration / 3600, 1) * 30 if s.duration >= _MIN_MOVIE_SECONDS else -60
                s.why.append(f"{int(s.duration // 3600)}h{int(s.duration % 3600 // 60):02d}m" if s.duration >= 3600 else f"{int(s.duration // 60)}m{int(s.duration % 60):02d}s")
                if s.duration < _MIN_MOVIE_SECONDS:
                    s.why.append("too short for a movie: preview / ad")
                if "live / no end marker" in s.why:
                    score -= 15
        else:
            score += min(s.size / (500 * 1048576), 1) * 30 if s.size else 0
            if s.size and s.size < 5 * 1048576:
                score -= 40
                s.why.append("tiny file: preview / ad")
        s.score = round(score, 1)
    return sorted(found.values(), key=lambda s: (-s.score, -len(s.why)))


def format_streams(streams: list[Stream], full: bool = False) -> str:
    if not streams:
        return "(none)"
    rows = []
    for i, s in enumerate(streams[:12 if not full else 60], 1):
        rows.append(f"{i:2d}. score {s.score:5.1f}  {s.kind:4}  {'REQUESTED' if s.observed else 'mentioned'}  {s.url}")
        rows.append(f"      {'; '.join(dict.fromkeys(s.why)) or '-'}")
        if full:
            rows.append("      headers: " + ", ".join(f"{k}={'‹hidden›' if k.lower() == 'cookie' else v}" for k, v in s.headers.items()))
            if s.source:
                rows.append(f"      source: {s.source}")
    return "\n".join(rows)


# ---------------------------------------------------------------- check + hand over
async def verify(s: Stream, cap: Capture) -> dict | None:
    """Does this candidate really download? Tries the browser's headers first, then the usual referer variants."""
    tries = [s.headers] + [h for h in m3u8._header_variants(cap.page_url, s.headers.get("Referer", "")) if h != s.headers]
    for h in tries[:6]:
        h = {**h, **({"Cookie": s.headers["Cookie"]} if "Cookie" in s.headers else {})}
        ok = await asyncio.to_thread(m3u8.playable if s.kind == "hls" else m3u8.playable_file, s.url, h)
        if ok:
            return h
    return None


async def pick(cap: Capture, streams: list[Stream], limit: int = 6) -> list[tuple[Stream, dict]]:
    """The candidates that really work, best first."""
    good: list[tuple[Stream, dict]] = []
    for s in streams[:limit]:
        if s.score < 20:
            continue
        h = await verify(s, cap)
        cap.notes.append(f"check {s.url[:110]}: " + ("downloadable" if h else "refused / not a playlist"))
        if h:
            good.append((s, h))
    return good


async def run(url: str, referer: str = "", on_status=None) -> tuple[Capture, list[Stream], list[tuple[Stream, dict]]]:
    """Record the page (one browser at a time), rank the streams in the recording, check which really download.
    -> (the recording, every candidate best first, the working ones best first with the headers that work)"""
    async with _LOCK:
        cap = await asyncio.wait_for(record(url, referer, on_status=on_status), config.CAPTURE_SECONDS + 90)
    streams = analyze(cap)
    return cap, streams, await pick(cap, streams)


async def extract(url: str, referer: str, dump=None, trace: list | None = None, alternates: list | None = None,
                  on_status=None) -> tuple[str, dict] | None:
    """Record the page in a browser and return the best working stream: (url, headers), or None."""
    trace = trace if trace is not None else []
    ev = dump.ev if dump is not None else (lambda s: None)
    if not available():
        msg = "browser capture is not installed: pip install playwright && playwright install chromium"
        ev(msg)
        trace.append(msg)
        return None
    try:
        cap, streams, good = await run(url, referer, on_status)
    except Exception as exc:  # noqa: BLE001
        msg = f"browser capture failed: {type(exc).__name__}: {str(exc)[:200]}"
        ev(msg)
        trace.append(msg)
        return None
    ev(f"browser capture: {len(cap.entries)} requests ({cap.counts()}), {len(streams)} stream candidate(s)")
    for line in format_streams(streams).splitlines()[:16]:
        ev("   " + line)
    if dump is not None:
        dump.attach("capture", cap.files(streams))
    if not good:
        trace.append(f"browser capture: {len(cap.entries)} requests, "
                     + (f"{len(streams)} candidate(s) but none downloadable" if streams else "no stream in any of them")
                     + " (see capture/report.txt in the dump)")
        return None
    best, headers = good[0]
    if alternates is not None:
        alternates.extend(s.url for s, _ in good[1:] if s.kind == best.kind and s.url != best.url)
    ev(f"browser capture picked {best.url}  (score {best.score}: {'; '.join(dict.fromkeys(best.why))})")
    return best.url, headers
