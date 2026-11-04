"""Tests of the browser capture with a REAL headless Chromium against local fake embed sites.

    python tests/test_capture.py       (needs: playwright + its Chromium, ffmpeg; set PLAYWRIGHT_BROWSERS_PATH if needed)

The fake player works like a JavaScript player: iframe on another origin -> /api/token (sets a cookie) -> /api/source
(JSON with the stream) -> master playlist -> media playlist -> segments. The stream host answers 403 without that
cookie and the player's Referer, and the page also fires an ad request and loads a short trailer.
"""
import asyncio
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
os.environ.update(BOT_TOKEN="1:" + "a" * 35, API_HASH="h" * 20, OWNER_ID="100", BASE_URL="http://embed.test",
                  DL_MIN_INTERVAL_MS="10", DL_ROUNDS="1", MAX_PARALLEL="1", CAPTURE_MODE="fallback")
os.chdir(tempfile.mkdtemp())

try:
    import httpx  # noqa: F401
except ImportError:  # only the page extraction needs httpx; a urllib shim with the same interface is enough here
    shim = types.ModuleType("httpx")

    class _R:
        def __init__(self, status, body, url, headers):
            self.status_code, self.text, self.url, self.headers = status, body.decode("utf-8", "ignore"), url, {k.lower(): v for k, v in headers.items()}

    class AsyncClient:
        def __init__(self, timeout=20, follow_redirects=True, proxy=None):
            self.cookies = types.SimpleNamespace(set=lambda *a, **k: None)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, headers=None, timeout=None):
            def run():
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=15) as r:
                        return _R(r.status, r.read(), r.geturl(), dict(r.headers))
                except urllib.error.HTTPError as e:
                    return _R(e.code, e.read(), url, dict(e.headers))
            return await asyncio.to_thread(run)

    shim.AsyncClient = AsyncClient
    sys.modules["httpx"] = shim
for n in ("pyrogram", "pyrogram.types", "pyrogram.errors", "yt_dlp"):
    sys.modules.setdefault(n, types.ModuleType(n))
class _Any:
    def __init__(self, *a, **k): pass
sys.modules["pyrogram.types"].InlineKeyboardButton = _Any
sys.modules["pyrogram.types"].InlineKeyboardMarkup = _Any
class RPCError(Exception): pass
sys.modules["pyrogram.errors"].RPCError = RPCError

import capture  # noqa: E402
import config  # noqa: E402
import debuglog  # noqa: E402
import downloader  # noqa: E402
import m3u8  # noqa: E402

STREAM = os.environ.get("HLS_DIR", "/tmp/hls")
S: dict = {"hits": []}
TOKEN = "tok123"


def player_js(auto: bool) -> str:
    body = """
async function play() {
  const t = await (await fetch('/api/token')).json();
  fetch('/ads/preroll.m3u8').catch(() => {});
  const s = await (await fetch('/api/source?token=' + t.token)).json();
  await fetch(s.trailer);                                   // a 30 s preview first
  const master = await (await fetch(s.hls)).text();
  const vurl = new URL(master.split('\\n').find(l => l && l[0] !== '#'), s.hls).href;
  const media = await (await fetch(vurl)).text();
  for (const l of media.split('\\n').filter(l => l && l[0] !== '#').slice(0, 3)) await fetch(new URL(l, vurl).href);
  document.title = 'playing';
}
"""
    return body + ("play();" if auto else "document.getElementById('go').onclick = () => { window.open('/ads/popup.html'); play(); };")


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def send(self, code, body, ctype="text/html", extra=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        path, port = self.path.split("?")[0], self.server.server_address[1]
        S["hits"].append((path, dict(self.headers)))
        if path in ("/embed/movie/auto", "/embed/movie/click"):      # the embed page: just an iframe on another origin
            return self.send(200, f'<html><body><iframe src="http://localhost:{port}/player/{path.rsplit("/", 1)[1]}" width=900 height=500></iframe></body></html>')
        if path == "/player/auto":
            return self.send(200, f"<html><body><div id=player>Loading…</div><script>{player_js(True)}</script></body></html>")
        if path == "/player/click":
            return self.send(200, f"<html><body><button id=go class=play-btn style='width:300px;height:100px'>PLAY</button><script>{player_js(False)}</script></body></html>")
        if path == "/embed/movie/nothing":
            return self.send(200, "<html><body>This video is unavailable.<script>console.log('hi')</script></body></html>")
        if path == "/api/token":
            return self.send(200, json.dumps({"token": TOKEN}), "application/json", {"Set-Cookie": "sess=abc123; Path=/; HttpOnly"})
        if path == "/api/source":
            base = f"http://localhost:{port}/hls/{TOKEN}"
            return self.send(200, json.dumps({"hls": f"{base}/master.m3u8", "trailer": f"{base}/trailer.m3u8", "title": "Fake Movie"}), "application/json")
        if path.startswith("/ads/"):
            return self.send(200, "#EXTM3U\n#EXTINF:15,\nad.ts\n#EXT-X-ENDLIST\n", "application/vnd.apple.mpegurl")
        if path.startswith(f"/hls/{TOKEN}/"):                         # the stream host: needs the cookie AND the player's Referer
            ok = "sess=abc123" in self.headers.get("Cookie", "") and self.headers.get("Referer", "").startswith(f"http://localhost:{port}/player/")
            if not ok:
                S["denied"] = S.get("denied", 0) + 1
                return self.send(403, "forbidden")
            name = path.split(f"/hls/{TOKEN}/", 1)[1]
            if name == "master.m3u8":
                return self.send(200, "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nv/index.m3u8\n", "application/vnd.apple.mpegurl")
            if name == "trailer.m3u8":
                return self.send(200, "#EXTM3U\n#EXT-X-TARGETDURATION:10\n" + "#EXTINF:10,\nt.ts\n" * 3 + "#EXT-X-ENDLIST\n", "application/vnd.apple.mpegurl")
            if name.startswith("v/"):
                f = os.path.join(STREAM, name[2:])
                if os.path.isfile(f):
                    return self.send(200, open(f, "rb").read(), "application/vnd.apple.mpegurl" if f.endswith("m3u8") else "video/mp2t")
        self.send(404, "nope")


fails = 0
def check(label, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    fails += 0 if cond else 1


# ------------------------------------------------------------------ analyzer on a synthetic recording (no browser)
def synthetic():
    cap = capture.Capture("http://page.test/embed")
    def ent(url, rtype="fetch", ctype="", body=None, status=200, size=0, headers=None):
        e = capture.Entry(len(cap.entries) + 1, "GET", url, rtype, headers or {"referer": "http://page.test/player"}, status=status, ctype=ctype, body=body, size=size, t0=1, t1=2)
        cap.entries.append(e); return e
    def media(n, dur): return "#EXTM3U\n#EXT-X-TARGETDURATION:10\n" + f"#EXTINF:{dur},\ns.ts\n" * n + "#EXT-X-ENDLIST\n"
    ent("http://cdn.test/pre/ad/preroll.m3u8", ctype="application/vnd.apple.mpegurl", body=media(2, 7))          # an ad
    ent("http://cdn.test/trailer/index.m3u8", ctype="application/vnd.apple.mpegurl", body=media(6, 10))          # 60 s preview
    ent("http://cdn.test/movie/master.m3u8", ctype="application/vnd.apple.mpegurl", body="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\nh/index.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\nm/index.m3u8\n")
    ent("http://cdn.test/live/index.m3u8", ctype="application/x-mpegurl", body="#EXTM3U\n" + "#EXTINF:6,\na.ts\n" * 100)  # live: no ENDLIST
    ent("http://cdn.test/movie/h/seg001.ts", rtype="media", ctype="video/mp2t")                                  # segments never count
    ent("http://cdn.test/hidden/x", rtype="xhr", ctype="application/octet-stream", body="#EXTM3U\n#EXTINF:5400,\nlong.ts\n#EXT-X-ENDLIST\n")   # a playlist with no .m3u8 in its name
    ent("http://cdn.test/big.mp4", rtype="media", ctype="video/mp4", size=900 * 1048576)
    ent("http://cdn.test/tiny.mp4", rtype="media", ctype="video/mp4", size=300_000)
    ent("http://api.test/v?id=1", ctype="application/json", body='{"sources":[{"file":"http://mention.test/m/master.m3u8"}],"dead":"http://x.test/g.m3u8"}')
    ent("http://cdn.test/gone.m3u8", status=404)
    return cap


async def main():
    # ---------- A. the analyzer
    cap = synthetic()
    streams = capture.analyze(cap)
    by = {s.url: s for s in streams}
    rank = [s.url.split("test/", 1)[1] for s in streams]
    check("analyzer: movie master, the unnamed 90-minute playlist and the big mp4 come first; the live stream next",
          set(rank[:3]) == {"movie/master.m3u8", "hidden/x", "big.mp4"} and rank[3] == "live/index.m3u8", str(rank))
    check("   the 60 s preview and the ad are last (ad below preview)", rank[-2:] == ["trailer/index.m3u8", "pre/ad/preroll.m3u8"] or rank[-3:-1] == ["trailer/index.m3u8", "pre/ad/preroll.m3u8"] or (rank.index("pre/ad/preroll.m3u8") > rank.index("trailer/index.m3u8") > 3), str(rank))
    check("   a playlist is recognised by its content, not only its name", "hidden/x" in rank and by["http://cdn.test/hidden/x"].duration == 5400)
    check("   the ad and the 60 s preview are marked as such", any("ad / tracker" in w for w in by["http://cdn.test/pre/ad/preroll.m3u8"].why) and any("too short" in w for w in by["http://cdn.test/trailer/index.m3u8"].why))
    check("   segments and 404s are ignored; a URL only mentioned in JSON is listed (lower than a requested one)",
          not any("seg001" in u or "gone" in u for u in by) and "http://mention.test/m/master.m3u8" in by and not by["http://mention.test/m/master.m3u8"].observed
          and by["http://mention.test/m/master.m3u8"].score < by["http://cdn.test/movie/master.m3u8"].score)
    check("   a big mp4 outranks a tiny one", by["http://cdn.test/big.mp4"].score > by["http://cdn.test/tiny.mp4"].score + 30)
    check("   quality ladder is read from the master", by["http://cdn.test/movie/master.m3u8"].heights == [1080, 720])

    # ---------- B. a real browser, a player that starts by itself
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    config.CAPTURE_SECONDS = 14
    page = f"http://localhost:{port}/embed/movie/auto"
    t0 = time.time()
    cap, streams, good = await capture.run(page, "http://embed.test/")
    urls = [e.url for e in cap.entries]
    check("browser: every request of the page and its iframe is recorded", all(any(p in u for u in urls) for p in ("/embed/movie/auto", "/player/auto", "/api/token", "/api/source", "master.m3u8", "v/index.m3u8", ".ts")), f"{len(cap.entries)} requests in {time.time() - t0:.1f}s: {cap.counts()}")
    check("browser: JSON / playlist response bodies were kept", any(e.body and "tok123" in e.body for e in cap.entries if "/api/source" in e.url) and any(e.body and e.body.startswith("#EXTM3U") for e in cap.entries))
    check("browser: the ad request was blocked", any(e.failed == "blocked" and "/ads/" in e.url for e in cap.entries))
    top = good[0][0] if good else None
    check("browser: the best stream is the movie master, found although no HTML or script names it", bool(top) and top.url.endswith("/hls/tok123/master.m3u8"), str(top and top.url))
    check("   the trailer ranks below it", all(s.score < top.score for s in streams if "trailer" in s.url) if top else False)
    h = good[0][1] if good else {}
    check("   the headers to download it carry the browser's cookie and the player's Referer", "sess=abc123" in h.get("Cookie", "") and "/player/auto" in h.get("Referer", ""), str({k: v for k, v in h.items() if k != "User-Agent"}))
    check("   the stream host accepted every request the page made (no 403 from a missing cookie / referer)", S.get("denied", 0) == 0, f"{S.get('denied', 0)} refused")
    files = cap.files(streams)
    har = json.loads(files["capture.har"])
    check("export: HAR 1.2 with every request, cookies hidden", len(har["log"]["entries"]) == len(cap.entries) and "abc123" not in files["capture.har"].decode())
    check("export: requests table, stream ranking and bodies are in the zip", {"report.txt", "requests.txt", "streams.txt", "cookies.txt"} <= set(files) and any(n.startswith("bodies/") for n in files) and "master.m3u8" in files["streams.txt"].decode())
    z = zipfile.ZipFile(io.BytesIO(cap.zip_bytes(streams)))
    check("export: zip opens", z.testzip() is None and "capture.har" in z.namelist())

    # ---------- C. the player waits for a click (and opens a popup ad)
    cap2, streams2, good2 = await capture.run(f"http://localhost:{port}/embed/movie/click", "http://embed.test/")
    check("browser: a player that waits for a click is started and found", bool(good2) and good2[0][0].url.endswith("master.m3u8"), f"{len(cap2.entries)} requests")
    check("   the popup ad was closed", any("popup closed" in n for n in cap2.notes), str(cap2.notes[:2]))

    # ---------- D. nothing on the page: ends by itself, says so
    config.CAPTURE_SECONDS = 8
    t0 = time.time()
    cap3, streams3, good3 = await capture.run(f"http://localhost:{port}/embed/movie/nothing", "http://embed.test/")
    check("browser: a page without any stream ends after CAPTURE_SECONDS with no result", not streams3 and not good3 and time.time() - t0 < 30, f"{time.time() - t0:.0f}s")
    config.CAPTURE_SECONDS = 14

    # ---------- E. through the normal extractor: static fails, the browser takes over; the dump has the capture
    dump, trace = m3u8.Dump(), []
    alts: list = []
    url, hdr = await m3u8.extract_m3u8(page, referer="http://embed.test/", trace=trace, dump=dump, alternates=alts)
    check("extract_m3u8: static extraction finds nothing, the browser capture does", bool(url) and url.endswith("master.m3u8") and "sess=abc123" in hdr.get("Cookie", ""), str(url))
    zd = zipfile.ZipFile(io.BytesIO(dump.zip_bytes()))
    check("   the extraction dump includes capture/capture.har and capture/report.txt", {"capture/capture.har", "capture/report.txt", "report.txt"} <= set(zd.namelist()))
    config.CAPTURE_MODE = "off"
    u2, _ = await m3u8.extract_m3u8(page, referer="http://embed.test/", trace=[], dump=m3u8.Dump())
    check("   with CAPTURE_MODE=off it does not start a browser", u2 is None)
    config.CAPTURE_MODE = "fallback"

    # ---------- F. the whole thing: embed page -> browser -> download with the cookie -> split -> upload
    sent, cards = [], []
    class Msg:
        def __init__(self, i=1): self.id = i
        async def edit_text(self, text, **k): cards.append(text)
    class Client:
        async def send_message(self, chat, text, **k):
            if chat != config.OWNER_ID: cards.append(text)
            return Msg()
        def stop_transmission(self): pass
        async def send_video(self, chat, path, progress=None, **k):
            size = os.path.getsize(path); progress(size, size); sent.append((os.path.basename(path), size, k.get("duration"))); return Msg(len(sent))
        send_document = send_video
    class Q:
        from_user = types.SimpleNamespace(id=555, first_name="Sam")
        message = types.SimpleNamespace(chat=types.SimpleNamespace(id=555))
        async def answer(self, *a, **k): pass
    config.MAX_UPLOAD_BYTES = 300 * 1024
    downloader.Card.INTERVAL = 0
    await downloader.start(Client(), Q(), "Fake Movie (2024)", page)
    for _ in range(900):
        if 555 not in downloader.BY_USER:
            break
        await asyncio.sleep(0.2)
    check("end to end: embed page -> browser capture -> download (cookie + referer) -> split -> upload", len(sent) >= 2 and "Sent" in cards[-1], f"{len(sent)} parts: {[s[1] // 1024 for s in sent]} KB; last card: {cards[-1].splitlines()[-1][:60]}")
    check("   the job trail says the stream came from the capture", any("master.m3u8" in l for l in debuglog.RECENT[0]["lines"]) and debuglog.RECENT[0]["state"] == "done")
    check("   the browser's session cookie never went to another host", all("Cookie" not in hd or "localhost" in hd.get("Host", "") or "127.0.0.1" in hd.get("Host", "") for _, hd in S["hits"] if "/hls/" in _))

    # ---------- G. a captured Cookie header must not follow a redirect to a different host
    got = {}
    class B(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):  # noqa: N802
            got["B"] = dict(self.headers); self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
    srvB = http.server.ThreadingHTTPServer(("127.0.0.1", 0), B)
    threading.Thread(target=srvB.serve_forever, daemon=True).start()
    class A(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):  # noqa: N802
            got["A"] = dict(self.headers)
            self.send_response(302); self.send_header("Location", f"http://127.0.0.1:{srvB.server_address[1]}/x"); self.send_header("Content-Length", "0"); self.end_headers()
    srvA = http.server.ThreadingHTTPServer(("localhost", 0), A)
    threading.Thread(target=srvA.serve_forever, daemon=True).start()
    net = m3u8._Net({"User-Agent": "x", "Cookie": "sess=SECRET"}, "")
    try:
        net.get(f"http://localhost:{srvA.server_address[1]}/start")
    finally:
        net.close()
    check("cookie scope: sent to the stream host, NOT to the host a redirect leads to", "sess=SECRET" in got["A"].get("Cookie", "") and "Cookie" not in got["B"], f"A: {got['A'].get('Cookie')}  B: {got['B'].get('Cookie')}")
    srvA.shutdown(); srvB.shutdown()

    srv.shutdown()
    print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
