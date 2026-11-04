"""m3u8 extraction from embed pages + HLS download + ffmpeg helpers.

Swap-friendly: if you have your own extractor, replace `extract_m3u8()` and keep
the signature `async (url, referer=None) -> (m3u8_url | None, headers)`.
"""
import asyncio
import io
import zipfile
import base64
import html
import json
import logging
import math
import os
import http.client
import random
import re
import shutil
import ssl
import subprocess
import threading
import time
from urllib.parse import unquote, urljoin, urlparse, urlsplit

import httpx

import config

log = logging.getLogger("m3u8")

# Same UA as the working browser/curl request: the stream host (a Cloudflare worker)
# only answers a mobile browser UA together with the player page as Referer.
UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36"
)

_M3U8_ABS = re.compile(
    r"""https?://[^\s"'<>\\()\[\]{}]+?\.m3u8(?:\?[^\s"'<>\\()\[\]{}]*)?""", re.I
)
_M3U8_REL = re.compile(r"""["']([^"'\s<>]+?\.m3u8(?:\?[^"'\s<>]*)?)["']""", re.I)
_IFRAME = re.compile(r"""<iframe[^>]+?src=["']([^"']+)["']""", re.I)
_ATOB = re.compile(r"""atob\(\s*["']([A-Za-z0-9+/=_-]{16,})["']\s*\)""")
_PACKED = re.compile(r"}\('(.*)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)", re.S)
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ------------------------------------------------------------- extraction
def _base_n(num: int, base: int) -> str:
    if num == 0:
        return "0"
    out = ""
    while num:
        out = _DIGITS[num % base] + out
        num //= base
    return out


def unpack_packed(js: str) -> list[str]:
    """Undo Dean Edwards `eval(function(p,a,c,k,e,d){...})` packed JavaScript."""
    results = []
    for m in _PACKED.finditer(js):
        payload, base, count, keys = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")
        payload = payload.replace("\\'", "'").replace("\\\\", "\\")
        table = {}
        for i in range(count):
            word = keys[i] if i < len(keys) and keys[i] else _base_n(i, base)
            table[_base_n(i, base)] = word
        results.append(re.sub(r"\b\w+\b", lambda mm: table.get(mm.group(0), mm.group(0)), payload))
    return results


def _clean(text: str) -> str:
    text = text.replace("\\/", "/").replace("\\u0026", "&").replace("\\u002F", "/")
    return html.unescape(text)


def find_m3u8_urls(text: str, base: str = "") -> list[str]:
    """All .m3u8 URLs in a page: plain, JSON-escaped, atob()'d or inside packed JS."""
    blobs = [_clean(text)]
    for b64 in _ATOB.findall(text):
        try:
            raw = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", "ignore")
            blobs.append(_clean(raw))
        except Exception:  # noqa: BLE001
            pass
    for unpacked in unpack_packed(text):
        blobs.append(_clean(unpacked))

    found: list[str] = []
    for blob in blobs:
        found += _M3U8_ABS.findall(blob)
        for rel in _M3U8_REL.findall(blob):
            found.append(urljoin(base, rel) if base else rel)

    seen, out = set(), []
    for u in found:
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
    out.sort(key=lambda u: 0 if re.search(r"master|index|playlist", u, re.I) else 1)
    return out


_SCRIPT_SRC = re.compile(r"""<script[^>]+?src=["']([^"']+)["']""", re.I)
_DATA_SRC = re.compile(r"""data-(?:src|url|embed|iframe)=["']([^"']+)["']""", re.I)
_JS_SRC = re.compile(r"""\.src\s*=\s*["']([^"']+)["']""")
_ABS_URL = re.compile(r"""https?://[^\s"'<>\\()]+""")
_ASSET = re.compile(r"\.(?:js|css|png|jpe?g|gif|svg|ico|woff2?|ttf|mp4|ts|json|m3u8)(?:\?|$)", re.I)


def _headers_for(page_url: str, browser: bool = False) -> dict:
    """Exactly what the browser/curl sends: UA + Referer (no Origin, no extras). `browser=True` adds the
    headers a real browser sends on a cross-site fetch (Origin, Accept-Language, Sec-Fetch-*), which some
    CDN rules insist on."""
    h = {"User-Agent": UA, "Accept": "*/*"}
    if page_url:
        h["Referer"] = page_url
    if browser:
        h.update(_BROWSER_HEADERS)
        origin = _origin(page_url)
        if origin:
            h["Origin"] = origin
    return h


_BROWSER_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}


def _origin(url: str) -> str:
    s = urlsplit(url or "")
    return f"{s.scheme}://{s.netloc}" if s.scheme and s.netloc else ""


def _retry_after(value) -> float:
    try:
        return min(float(value), 60.0)
    except (TypeError, ValueError):
        return 0.0


async def _get_patient(http, u: str, headers: dict, timeout: float | None = None, tries: int = 3):
    """GET that waits and tries again when the host answers 429 (Retry-After is honoured, capped)."""
    r = None
    for i in range(tries):
        kw = {"timeout": timeout} if timeout else {}
        r = await http.get(u, headers=headers, **kw)
        if r.status_code != 429 or i == tries - 1:
            return r
        await asyncio.sleep(min(_retry_after(r.headers.get("retry-after")) or 2.0 * (i + 1), 15.0) * random.uniform(0.8, 1.2))
    return r


def _header_variants(page_url: str, *more: str) -> list[dict]:
    """Header sets to try when asking for a playlist: the page it was found on, any extra
    referers (e.g. the top-level embed page), the configured default player referer,
    then no referer at all."""
    out: list[dict] = []
    refs = (page_url, *more, config.M3U8_REFERER, "")
    for ref in refs:
        h = _headers_for(ref)
        if h not in out:
            out.append(h)
    for ref in refs[:2]:  # last resort: the same referers with the full browser header set
        h = _headers_for(ref, browser=True)
        if ref and h not in out:
            out.append(h)
    return out


def _looks_like_page(u: str) -> bool:
    path = urlparse(u).path.lower()
    return path.endswith((".html", ".htm", ".php")) or any(
        k in path for k in ("/embed", "/player", "/iframe", "/e/", "/v/", "/watch", "/rcp", "/prorcp", "/srcrcp", "/play")
    )


def find_sub_pages(text: str, base: str) -> list[str]:
    """Player pages referenced by this page: <iframe src>, data-src, `x.src = "..."`
    set from JavaScript, or absolute URLs that look like an embed/player page."""
    t = _clean(text)
    found = [urljoin(base, html.unescape(x)) for x in _IFRAME.findall(text)]
    found += [urljoin(base, x) for x in _IFRAME.findall(t.replace('\\"', '"'))]  # iframe HTML inside JSON
    found += [urljoin(base, x) for x in _DATA_SRC.findall(t)]
    found += [urljoin(base, x) for x in _JS_SRC.findall(t)]
    found += [u for u in _ABS_URL.findall(t) if _looks_like_page(u)]
    found += find_more_sub_pages(text, base)
    seen, out = {base}, []
    for u in found:
        if u.startswith("http") and u not in seen and not _ASSET.search(u):
            seen.add(u)
            out.append(u)
    out.sort(key=lambda u: 0 if _PAGE_WORDS.search(urlparse(u).path) else 1)  # player-looking pages first
    return out


# --- the embed page hides its mirrors: base64(XOR(url, NONCE)) in SOURCES / AV123_SOURCES
_NONCE = re.compile(r"""\bNONCE\s*=\s*["']([^"']{8,128})["']""")
_B64_STR = re.compile(r"""["']([A-Za-z0-9+/]{40,}={0,2})["']""")


_MEDIA_EXT = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
_MEDIA_URL = re.compile(r"""https?://[^\s"'<>\\]+?\.(?:mp4|mkv|webm|mov|m4v)(?:\?[^\s"'<>\\]*)?""", re.I)


def is_file_url(u: str) -> bool:
    """A direct video file (progressive download) instead of an HLS playlist."""
    return urlsplit(u).path.lower().endswith(_MEDIA_EXT)


def _quality_order(key: str) -> tuple:
    """HSTORAGE_CANDIDATES keys: best allowed height first, then 'default', 'raw', then taller than allowed."""
    if str(key).isdigit():
        n = int(key)
        return (0, -n) if (not config.MAX_HEIGHT or n <= config.MAX_HEIGHT) else (3, n)
    return {"default": (1, 0), "raw": (2, 0)}.get(str(key), (2, 1))


def find_direct_files(texts: list[str]) -> list[str]:
    """Direct video files an embed page offers: the player's HSTORAGE_CANDIDATES (1080/720/default/raw),
    hidden (obfuscated) mp4 URLs, and plain .mp4/.mkv/.webm URLs - best quality first."""
    out: list[str] = []
    for t in texts:
        for m in re.finditer(r"HSTORAGE_CANDIDATES\s*=\s*(\{.*?\})\s*;", t, re.S):
            try:
                cands = json.loads(m.group(1))
            except ValueError:
                continue
            out += [v for _, v in sorted(((k, v) for k, v in cands.items() if isinstance(v, str) and v.startswith("http")),
                                         key=lambda kv: _quality_order(kv[0]))]
        out += find_obfuscated_urls([t], media=True)
    for t in texts:
        out += [u.replace("\\/", "/") for u in _MEDIA_URL.findall(_clean(t))]
    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def find_obfuscated_urls(texts: list[str], media: bool = False) -> list[str]:
    """Decode the stream URLs the embed page hides the way its own player does
    (`atob(x)` XOR-ed with the page's NONCE). Returns .m3u8 URLs in page order:
    main source first, then its mirrors/fallbacks, then the other qualities/servers."""
    out: list[str] = []
    for t in texts:
        m = _NONCE.search(t)
        if not m:
            continue
        key = m.group(1).encode()
        for b64 in _B64_STR.findall(t):
            try:
                raw = base64.b64decode(b64 + "=" * (-len(b64) % 4))
                text = bytes(c ^ key[i % len(key)] for i, c in enumerate(raw)).decode("utf-8")
            except Exception:  # noqa: BLE001
                continue
            wanted = is_file_url(text) if media else ".m3u8" in text.lower()
            if re.fullmatch(r"https?://\S+", text) and wanted and text not in out:
                out.append(text)
    return out


# --- runtime-built players: endpoints the page calls, worker host + id synthesis
_API_CALLS = [
    re.compile(r"""fetch\(\s*[`"']([^`"']+)[`"']"""),
    re.compile(r"""\.(?:get|post|getJSON|ajax|request)\(\s*[`"']([^`"']+)[`"']"""),
    re.compile(r"""\burl\s*:\s*[`"']([^`"']+)[`"']"""),
    re.compile(r"""\.open\(\s*[`"'](?:GET|POST)[`"']\s*,\s*[`"']([^`"']+)[`"']"""),
    re.compile(r"""[`"']((?:https?:)?//[^`"'\s<>]*?/(?:wp-json|api)/[^`"'\s<>]*)[`"']"""),
    re.compile(r"""[`"'](/(?:wp-json|api)/[^`"'\s<>]*)[`"']"""),
    re.compile(r"""[`"']([^`"'\s<>]*admin-ajax\.php[^`"'\s<>]*)[`"']"""),
]
_SKIP_HOST = re.compile(
    r"google|gstatic|doubleclick|facebook|twitter|cloudflareinsights|jsdelivr|cdnjs|unpkg|"
    r"bootstrapcdn|jquery|fontawesome|w3\.org|schema\.org|wp\.com|gravatar",
    re.I,
)

# Ad / tracker / pre-roll URLs: never the movie. The static extractor skips them, the browser capture blocks them.
AD_RE = re.compile(
    r"doubleclick|googlesyndication|googleadservices|adservice|adsystem|adnxs|popads|popcash|propeller|exoclick|exosrv|adsterra|"
    r"juicyads|trafficjunky|clickadu|hilltopads|onclkds|a-ads|adcash|admaven|bidgear|monetag|richads|yllix|mgid|taboola|outbrain|"
    r"imasdk|adform|criteo|pubmatic|rubicon|openx|amazon-adsystem|/vast\b|vast\.xml|[./_-]vast[./_-]|/ads?/|adserver|preroll|midroll|"
    r"google-analytics|googletagmanager|histats|clarity\.ms|hotjar", re.I)
_WORKER_HOST = re.compile(r"https?://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:workers|pages)\.dev", re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def find_api_endpoints(texts: list[str], base: str) -> list[str]:
    """URLs the page's JavaScript calls (fetch/ajax/wp-json/api) - players often get the
    stream address from one of them instead of having it in the HTML."""
    found: list[str] = []
    for t in texts:
        t = _clean(t)
        for rx in _API_CALLS:
            for m in rx.findall(t):
                if "${" in m or "{{" in m or m.startswith(("data:", "javascript:", "#", "mailto:")):
                    continue
                u = urljoin(base, m)
                if (
                    u.startswith("http") and u != base and not _ASSET.search(u)
                    and not _SKIP_HOST.search(urlparse(u).netloc) and u not in found
                ):
                    found.append(u)
    found.sort(key=lambda u: 0 if re.search(r"stream|source|player|video|embed|playlist|m3u8|hls", u, re.I) else 1)
    return found


def synth_candidates(texts: list[str]) -> list[str]:
    """Some players serve `https://<worker>.workers.dev/<uuid>/playlist.m3u8` where the host and the
    uuid sit in different places (page, JSON). Combine every host with every uuid."""
    blob = "\n".join(_clean(t) for t in texts)
    hosts = list(dict.fromkeys(_WORKER_HOST.findall(blob)))
    ids = list(dict.fromkeys(i.lower() for i in _UUID.findall(blob)))
    return [f"{h}/{i}/playlist.m3u8" for h in hosts for i in ids]


# --- pages that bounce visitors to the home page (referer / cookie / login gated embeds)
_PLAYER_MARKS = re.compile(
    r"NONCE\s*=|SOURCES\s*=|\.m3u8|<video\b|<source\b|new\s+Hls\b|Hls\.isSupported|jwplayer\(|videojs\(", re.I
)


def looks_like_player(text: str) -> bool:
    return bool(_PLAYER_MARKS.search(text or ""))


def _bounced(requested: str, r) -> bool:
    """True if the server redirected us to the site's home page instead of the page we asked for."""
    final, asked = urlsplit(str(r.url)), urlsplit(requested)
    if str(r.url) == requested:
        return False
    return final.path.strip("/") == "" and not final.query and bool(asked.query or asked.path.strip("/"))


def _ref_label(ref: str, url: str) -> str:
    if not ref:
        return "none"
    return "itself" if ref == url else ref


def referer_variants(u: str, ref: str | None, hints: str = "") -> list[str]:
    """Referers to try for a page that may only open for the right visitor: the one we were given,
    the site root, the post's own page, the sibling APIJAV sites, and finally none."""
    s = urlsplit(u)
    out = [ref or u, f"{s.scheme}://{s.netloc}/"]
    for m in re.findall(r'"(?:link|url|permalink)"\s*:\s*"(https?:[^"]+)"', hints or "")[:2]:
        out.append(_clean(m))
    for v in config.SOURCES.values():
        b = urlsplit(v["base"])
        out.append(f"{b.scheme}://{b.netloc}/")
    out += [config.M3U8_REFERER, ""]
    seen, res = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            res.append(r)
    return res


def _set_cookies(http, url: str, cookies: dict | None) -> None:
    """Put the site cookies in the client's jar for the page host and its parent domain."""
    if not cookies:
        return
    host = urlsplit(url).hostname or ""
    parts = host.split(".")
    is_ip = bool(re.fullmatch(r"[\d.]+", host)) or ":" in host
    domains = [host] + ([".".join(parts[-2:])] if len(parts) > 2 and not is_ip else [])
    for name, value in cookies.items():
        for d in domains:
            http.cookies.set(name, value, domain=d)


# ------------------------------------------------------------- what the extractor saw (for /probe and /debug)
class Dump:
    """Every request the extractor made and every decision it took. `zip_bytes()` packs it for the owner:
    report.txt (the story) + one file per page / script / API answer (the raw text), so a failing embed page
    can be read instead of guessed."""
    MAX_BODY = 300_000

    def __init__(self):
        self.t0 = time.monotonic()
        self.events: list[str] = []
        self.pages: list[dict] = []
        self.extra: dict[str, bytes] = {}   # more files for the zip (the browser capture)

    def attach(self, folder: str, files: dict) -> None:
        for name, data in files.items():
            self.extra[f"{folder}/{name}"] = data if isinstance(data, bytes) else str(data).encode()

    def ev(self, text: str) -> None:
        self.events.append(f"+{time.monotonic() - self.t0:6.1f}s  {text}")

    def page(self, url: str, ref: str, status, final: str, headers, body: str, note: str = "") -> None:
        h = {str(k).lower(): v for k, v in dict(headers or {}).items()}
        self.pages.append({
            "n": len(self.pages) + 1, "url": url, "ref": ref, "status": status, "final": final, "note": note,
            "ctype": h.get("content-type", ""), "server": h.get("server", ""), "size": len(body or ""),
            "body": (body or "")[: self.MAX_BODY],
        })
        self.ev(f"GET {url}  Referer={ref or '-'}  -> HTTP {status}, {len(body or '')} bytes"
                + (f", redirected to {final}" if final and final != url else "") + (f"  [{note}]" if note else ""))

    @staticmethod
    def _fname(p: dict) -> str:
        u = urlsplit(p["url"])
        tail = re.sub(r"[^A-Za-z0-9._-]+", "_", (u.netloc + u.path))[:70].strip("_")
        return f"pages/{p['n']:02d}_{tail}.txt"

    def report(self, scrub=lambda t: t) -> str:
        out = ["EXTRACTION REPORT", "", "Pages, scripts and API answers fetched:"]
        for p in self.pages:
            out.append(f" {p['n']:02d} HTTP {p['status']}  {p['size']:>7} bytes  {p['ctype'][:30]:30}  server={p['server'] or '-'}  {p['url']}")
            out.append(f"      file: {self._fname(p)}" + (f"   note: {p['note']}" if p["note"] else ""))
        out += ["", "Timeline:"] + self.events
        return scrub("\n".join(out))

    def zip_bytes(self, scrub=lambda t: t) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("report.txt", self.report(scrub))
            for name, data in self.extra.items():
                z.writestr(name, data)
            for p in self.pages:
                z.writestr(self._fname(p), scrub(f"URL: {p['url']}\nFinal: {p['final']}\nReferer: {p['ref']}\nHTTP {p['status']}  {p['ctype']}\n\n{p['body']}"))
        return buf.getvalue()


_CHALLENGE = re.compile(
    r"cf-chl|cf_chl_opt|Just a moment|Attention Required|challenge-platform|Checking your browser|ddos-guard|"
    r"DDoS protection by|Access denied|Enable JavaScript and cookies|are not a robot|_Incapsula_|sucuri", re.I)


def challenge_hint(text: str) -> str:
    """Non-empty when the page is a bot-protection screen (Cloudflare & co.) instead of the player."""
    t = text or ""
    if looks_like_player(t) and len(t) > 3000:
        return ""
    m = _CHALLENGE.search(t[:30000])
    return m.group(0) if m else ""


def _protect_msg(u: str, hint: str) -> str:
    return (f"{u} -> a bot-protection page ({hint}), not the player. "
            "Try: pip install curl_cffi, or BROWSER_FALLBACK=1 with playwright, or a PROXY")


class _Resp:
    """Looks like the part of an httpx response the extractor uses (for the curl_cffi fallback)."""

    def __init__(self, status_code: int, text: str, url: str, headers: dict):
        self.status_code, self.text, self.url = status_code, text, url
        self.headers = {str(k).lower(): v for k, v in headers.items()}


def _cffi_page(u: str, headers: dict, cookies: dict | None = None) -> "_Resp | None":
    """GET a page with Chrome's TLS fingerprint (pip install curl_cffi). For sites whose bot protection
    refuses Python's handshake whatever the headers are."""
    try:
        from curl_cffi import requests as cr  # noqa: PLC0415
        hdrs = {k: v for k, v in headers.items() if k.lower() not in ("user-agent", "accept-encoding", "connection")}
        proxy = (config.PROXIES or [None])[0]
        r = cr.get(u, headers=hdrs, impersonate=os.getenv("IMPERSONATE", "chrome"), timeout=25, allow_redirects=True,
                   cookies=cookies or None, proxies={"http": proxy, "https": proxy} if proxy else None)
        return _Resp(r.status_code, r.text, str(r.url), dict(r.headers))
    except Exception as exc:  # noqa: BLE001
        log.info("curl_cffi page fetch failed: %s", type(exc).__name__)
        return None


# --- more ways a page hands over the next frame / the stream
_PROP_SRC = re.compile(r"""\bsrc\s*:\s*[`"']([^`"']+)[`"']""")                                  # {src: "/prorcp/abc"}
_ATTR_SRC = re.compile(r"""\.attr\(\s*[`"'](?:src|href|data-src)[`"']\s*,\s*[`"']([^`"']+)[`"']""")  # $(f).attr("src", "...")
_LOC_JS = re.compile(r"""(?:\blocation(?:\.href)?\s*=|\blocation\.(?:replace|assign)\(|window\.open\()\s*[`"']([^`"']+)[`"']""")
_META_REFRESH = re.compile(r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["'][^"']*?url=([^"'>\s]+)""", re.I)
_PAGE_WORDS = re.compile(r"rcp|embed|player|iframe|stream|source|watch|play|video|/e/|/v/|/f/|/d/", re.I)
_STREAMISH = re.compile(
    r"""[`"'](https?://[^`"'\s<>\\{}]+/(?:playlist|master|index|manifest|hls|stream|m3u8|pl)(?:/[^`"'\s<>\\{}]*)?)[`"']""", re.I)
_TEMPLATE = re.compile(r"""https?://[^\s"'<>\\]*\{v\d+\}[^\s"'<>\\]*""")
_HOSTISH = re.compile(r"""\b((?:[a-z0-9-]+\.)+(?:com|net|org|io|cc|co|me|tv|xyz|site|online|cloud|live|pro|top|app|link|club|fun|info|to|sx|ru|in))\b""", re.I)


def find_more_sub_pages(text: str, base: str) -> list[str]:
    """Player pages that `find_sub_pages` misses: `{src: "/rcp/x"}`, `.attr("src", ...)`, `location = ...`, meta refresh.
    Relative paths count only when they look like a player page, so images and API paths are not followed."""
    t = _clean(text)
    found: list[str] = []
    for rx in (_PROP_SRC, _ATTR_SRC, _LOC_JS, _META_REFRESH):
        for m in rx.findall(t):
            m = html.unescape(m).strip()
            if not m or m.startswith(("data:", "javascript:", "#", "blob:")) or "${" in m or "{{" in m:
                continue
            u = urljoin(base, m)
            if u.startswith("http") and not _ASSET.search(u) and (_PAGE_WORDS.search(urlparse(u).path) or _looks_like_page(u)):
                found.append(u)
    return list(dict.fromkeys(found))


def find_streamish_urls(texts: list[str], base: str = "") -> list[str]:
    """URLs that are probably playlists but do not end in .m3u8 (`/playlist/123`, `/hls/abc/master`). They are
    only candidates: `verify()` must see `#EXTM3U` before one is used."""
    out: list[str] = []
    for t in texts:
        for u in _STREAMISH.findall(_clean(t)):
            if not _ASSET.search(u) and not _SKIP_HOST.search(urlparse(u).netloc) and u not in out:
                out.append(u)
    return out


def expand_templates(texts: list[str], page: str, limit: int = 24) -> tuple[list[str], list[str]]:
    """Players that write `https://tmstr.{v1}/pl/x/master.m3u8 or https://tmstr.{v2}/...` and fill {v1}.. in from a
    list of domains. We try the domains found in the page's own text. -> (candidates, the raw templates)"""
    blob = "\n".join(_clean(t) for t in texts)
    templates: list[str] = []
    for m in _TEMPLATE.findall(blob):
        for part in re.split(r"\s+or\s+", m):
            if "{v" in part and part not in templates:
                templates.append(part)
    if not templates:
        return [], []
    page_host = urlparse(page).netloc
    hosts = [h.lower() for h in dict.fromkeys(_HOSTISH.findall(blob))
             if not _SKIP_HOST.search(h) and h.lower() != page_host and not re.search(r"\.(?:js|css|png|jpg|svg)$", h, re.I)]
    cands: list[str] = []
    for t in templates[:3]:
        for h in hosts[:12]:
            c = re.sub(r"\{v\d+\}", h, t)
            if c not in cands:
                cands.append(c)
    return cands[:limit], templates


async def diagnose(url: str, referer: str | None = None, cookies: dict | None = None, hints: str = "") -> dict:
    """Open `url` with each referer variant and report what comes back. Used by /probe, the Test
    button and check_m3u8.py. -> {"ok", "lines", "referer", "streams"}"""
    lines: list[str] = []
    best, streams = None, 0
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, proxy=config.PROXY or None) as http:
        _set_cookies(http, url, cookies)
        for i, rf in enumerate(referer_variants(url, referer, hints), 1):
            label = _ref_label(rf, url)
            try:
                r = await http.get(url, headers=_headers_for(rf))
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{i}. Referer {label}: {type(exc).__name__}")
                continue
            final = str(r.url)
            if r.status_code >= 400:
                lines.append(f"{i}. Referer {label}: HTTP {r.status_code}")
                continue
            if _bounced(url, r):
                lines.append(f"{i}. Referer {label}: redirected to the home page ({final})")
                continue
            text = r.text
            n_obf = len(find_obfuscated_urls([text]))
            n_m3u8 = len(find_m3u8_urls(text, final))
            n_sub = len(find_sub_pages(text, final))
            n_files = len(find_direct_files([text]))
            player = looks_like_player(text)
            lines.append(f"{i}. Referer {label}: HTTP {r.status_code}, {len(text)} bytes, "
                         f"{'player markers' if player else 'no player markers'}, "
                         f"{n_obf} hidden + {n_m3u8} plain stream URLs, {n_files} direct video files, {n_sub} sub-pages")
            if best is None and (n_obf or n_m3u8 or n_files or player):
                best, streams = rf, n_obf + n_m3u8 + n_files
    return {"ok": best is not None, "lines": lines, "referer": best, "streams": streams}


async def extract_m3u8(
    url: str, referer: str | None = None, depth: int = 3, trace: list | None = None, hints: str | None = None,
    alternates: list | None = None, cookies: dict | None = None, dump: "Dump | None" = None, on_status=None,
):
    """Return (m3u8_url, headers) or (None, headers). `dump` (a Dump) records everything the extractor saw.

    Every candidate is fetched and must really be a playlist (`#EXTM3U`) before it is
    returned, so a wrong guess like a relative path on the wrong host is skipped.
    If the page has no working playlist it follows iframes / JS-created players.
    A direct .m3u8 link is checked too (with the given referer, the configured default
    referer, then none) and the header set that works is returned for the download.
    `alternates` (a list) receives the other mirrors the page offers, to retry if a download fails.
    `cookies` (name -> value) are sent to the page's site (login / age-check cookies). If a page
    bounces us to the home page, other Referers are tried before giving up.
    `hints` is extra text to search as if it were part of the page (e.g. the API's JSON for
    this post). `trace` (a list) receives one line per failed attempt, for error messages."""
    trace = trace if trace is not None else []
    url = url.strip().replace("&amp;", "&").replace("#038;", "&").replace("&#038;", "&")
    referer = referer.replace("&amp;", "&").replace("#038;", "&").replace("&#038;", "&") if referer else referer
    seen: set[str] = set()
    budget = [45]  # max pages + scripts + API calls fetched
    dump = dump if dump is not None else Dump()
    dump.ev(f"extract {url}  referer={referer or '-'}  depth={depth}  curl_cffi={'yes' if _cffi() else 'no'}  "
            f"browser capture={config.CAPTURE_MODE}")

    async with httpx.AsyncClient(
        timeout=20, follow_redirects=True, proxy=config.PROXY or None
    ) as http:

        _set_cookies(http, url, cookies)

        async def fetch(u: str, ref: str, variants: bool = False):
            """GET a page/script/API. With `variants`, a page that bounces us to the home page (or says
            401/403) is retried with other Referers."""
            if budget[0] <= 0:
                return None
            budget[0] -= 1
            refs = referer_variants(u, ref, hints or "") if variants else [ref]
            bounced = None
            for i, rf in enumerate(refs):
                try:
                    r = await _get_patient(http, u, _headers_for(rf))
                except Exception as exc:  # noqa: BLE001
                    trace.append(f"{u} -> {type(exc).__name__}")
                    dump.ev(f"GET {u}  Referer={rf or '-'}  -> {type(exc).__name__}: {str(exc)[:160]}")
                    return None
                note = ""
                if (r.status_code in (403, 429, 503) or challenge_hint(r.text)) and _cffi():
                    dump.ev(f"{u}: refused / challenged ({r.status_code}); trying again with a Chrome TLS fingerprint")
                    alt = await asyncio.to_thread(_cffi_page, u, _headers_for(rf), cookies)
                    if alt is not None and alt.status_code < 400:
                        r, note = alt, "fetched with curl_cffi"
                dump.page(u, rf, r.status_code, str(r.url), r.headers, r.text, note)
                if r.status_code >= 400:
                    trace.append(f"{u} -> HTTP {r.status_code}" + (f" (Referer {_ref_label(rf, u)})" if variants else ""))
                    if (hint := challenge_hint(r.text)) and not any("bot-protection" in x and u in x for x in trace):
                        trace.append(_protect_msg(u, hint))
                        dump.ev(_protect_msg(u, hint))
                    if variants and r.status_code in (401, 403) and i < len(refs) - 1:
                        continue
                    return None
                if variants and _bounced(u, r):
                    bounced = str(r.url)
                    continue
                if i:
                    log.info("page %s opened with Referer %s", u, _ref_label(rf, u))
                return r
            if bounced:
                trace.insert(0, f"{u} -> redirected to the home page ({bounced}) with every Referer tried"
                                + ("" if cookies else "; it may need a login or cookies (Settings → Site access)"))
            return None

        async def verify(u: str, page: str):
            codes: list[str] = []
            for hdr in _header_variants(page, referer or url):
                try:
                    r = await _get_patient(http, u, hdr, timeout=10)
                except Exception as exc:  # noqa: BLE001
                    trace.append(f"{u} -> {type(exc).__name__}")
                    return None
                if r.status_code == 200 and r.text.lstrip("\ufeff \r\n\t").startswith("#EXTM3U"):
                    dump.ev(f"verify {u}: PLAYLIST OK with Referer={hdr.get('Referer', '-')}")
                    return hdr
                code = f"HTTP {r.status_code}" + (" (not a playlist)" if r.status_code == 200 else "")
                if r.status_code == 200:
                    dump.ev(f"verify {u}: HTTP 200 but not a playlist, starts with: {r.text[:100]!r}")
                if code not in codes:
                    codes.append(code)
            trace.append(f"{u} -> {' / '.join(codes)}")
            return None

        async def verify_file(u: str, page: str):
            codes: list[str] = []
            for hdr in _header_variants(page, referer or url):
                try:
                    ok, _size, _ranges, status = await asyncio.to_thread(probe_file, u, hdr)
                except Exception as exc:  # noqa: BLE001
                    trace.append(f"{u} -> {type(exc).__name__}")
                    return None
                if ok:
                    return hdr
                codes.append(f"HTTP {status}" if status not in (200, 206) else "not a video")
            trace.append(f"{u} -> {' / '.join(dict.fromkeys(codes)) or 'no answer'}")
            return None

        if is_file_url(url):
            hdr = await verify_file(url, referer or config.M3U8_REFERER)
            return url, hdr or _headers_for(referer or config.M3U8_REFERER)

        if urlparse(url).path.lower().endswith(".m3u8"):
            hdr = await verify(url, referer or config.M3U8_REFERER)
            # Even if the check failed, let the downloaders try (with the best-guess headers).
            return url, hdr or _headers_for(referer or config.M3U8_REFERER)

        async def walk(u: str, ref: str, d: int):
            if u in seen or d < 0:
                return None
            seen.add(u)
            r = await fetch(u, ref, variants=True)
            if r is None:
                return None
            page = str(r.url)
            texts = [r.text]
            if hints and d == depth:
                texts.append(hints)
            if hint := challenge_hint(r.text):
                msg = _protect_msg(page, hint)
                trace.append(msg)
                dump.ev(msg)
            scripts = _SCRIPT_SRC.findall(r.text)
            dump.ev(f"page {page}: {len(r.text)} bytes, player markers={'yes' if looks_like_player(r.text) else 'no'}, "
                    f"{len(scripts)} script(s), {len(_IFRAME.findall(r.text))} iframe(s)")
            for src in scripts[:12]:  # the stream is often set in an external script
                su = urljoin(page, html.unescape(src))
                if _SKIP_HOST.search(urlparse(su).netloc):  # analytics / CDN libraries never hold the stream
                    continue
                sr = await fetch(su, page)
                if sr is not None:
                    texts.append(sr.text[:400_000])

            async def try_cands(blobs: list[str], at: str):
                cands: list[str] = []
                for t in blobs:
                    for c in find_m3u8_urls(t, at):
                        if c not in cands:
                            cands.append(c)
                for c in find_streamish_urls(blobs, at):  # playlists without a .m3u8 name
                    if c not in cands:
                        cands.append(c)
                ads = [c for c in cands if AD_RE.search(c)]
                if ads:
                    dump.ev(f"skipped {len(ads)} ad-looking address(es) on {at}: " + ", ".join(ads[:3]))
                    cands = [c for c in cands if c not in ads]
                if cands:
                    dump.ev(f"candidates on {at}: " + ", ".join(cands[:8]) + (f" (+{len(cands) - 8} more)" if len(cands) > 8 else ""))
                for c in cands[:8]:
                    hdr = await verify(c, page)
                    if hdr:
                        return c, hdr
                return None

            # the real stream list is hidden in the page (base64 + XOR with NONCE): mirrors in order
            decoded = [c for c in find_obfuscated_urls(texts) if not AD_RE.search(c)]
            for c in decoded[:10]:
                hdr = await verify(c, page)
                if hdr:
                    if alternates is not None:
                        alternates.extend(x for x in decoded if x != c)
                    return c, hdr

            res = await try_cands(texts, page)
            if res:
                return res

            # progressive video files (e.g. the Hentai player's HSTORAGE_CANDIDATES: 1080p / 720p / default / raw)
            files = find_direct_files(texts)
            for c in files[:6]:
                hdr = await verify_file(c, page)
                if hdr:
                    if alternates is not None:
                        alternates.extend(x for x in files if x != c)
                    return c, hdr

            # the player may ask an API for the stream (fetch/ajax/wp-json): read those answers too
            api_texts: list[str] = []
            for ep in find_api_endpoints(texts, page)[:6]:
                er = await fetch(ep, page)
                if er is not None:
                    api_texts.append(er.text[:400_000])
                    res = await try_cands([er.text], ep)
                    if res:
                        return res

            # worker host + uuid found in different places -> <host>/<uuid>/playlist.m3u8
            for c in synth_candidates(texts + api_texts)[:6]:
                hdr = await verify(c, page)
                if hdr:
                    return c, hdr
            texts += api_texts

            # `https://x.{v1}/.../master.m3u8` templates: fill the placeholders with the domains named in the page
            tcands, templates = expand_templates(texts, page)
            if templates:
                dump.ev(f"url templates on {page}: {templates[:3]}; trying {len(tcands)} filled-in address(es)")
                for c in tcands:
                    hdr = await verify(c, page)
                    if hdr:
                        return c, hdr

            subs: list[str] = []
            for t in texts:
                for sp in find_sub_pages(t, page):
                    if sp not in subs:
                        subs.append(sp)
            if subs:
                dump.ev(f"sub-pages on {page}: " + ", ".join(subs[:8]))
            for sp in subs[:8]:
                res = await walk(sp, page, d - 1)
                if res:
                    return res
            return None

        async def via_browser():
            """Record every request the page makes in a real browser and take the stream from that (capture.py)."""
            import capture  # noqa: PLC0415  (lazy: Playwright is optional)
            dump.ev("browser capture: opening the page in Chromium and recording the network")
            got = await capture.extract(url, referer or "", dump, trace, alternates, on_status)
            return got

        res = None
        if config.CAPTURE_MODE == "first":
            res = await via_browser()
        if not res:
            res = await walk(url, referer or url, depth)
        if not res and config.CAPTURE_MODE == "fallback":
            dump.ev("static extraction found nothing: recording the page in a browser")
            res = await via_browser()
        elif not res and config.CAPTURE_MODE == "off":
            dump.ev("browser capture is off (CAPTURE_MODE=fallback turns it on; pip install playwright && playwright install chromium)")
        dump.ev("RESULT: " + (res[0] if res else "no stream found"))
    return res if res else (None, _headers_for(referer or url))


# ------------------------------------------------------------- ffmpeg helpers
class DiskSpaceError(RuntimeError):
    """Not enough free disk space for the download (small hosts, e.g. free tiers)."""


def free_mb(path: str) -> float:
    try:
        return shutil.disk_usage(path if os.path.exists(path) else (os.path.dirname(path) or ".")).free / 1048576
    except OSError:
        return float("inf")


class ToolMissing(RuntimeError):
    """ffmpeg / ffprobe is not installed (and no bundled copy is available)."""


_tools: dict[str, str | None] = {}


def tool(name: str) -> str | None:
    """Path of `ffmpeg` / `ffprobe`: the system one, else (ffmpeg only) the static binary bundled in the
    `imageio-ffmpeg` pip package - that is what makes buildpack hosts (Koyeb, Heroku...) work without apt."""
    if name not in _tools:
        path = shutil.which(name)
        if not path and name == "ffmpeg":
            try:
                import imageio_ffmpeg
                path = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:  # noqa: BLE001
                path = None
        _tools[name] = path if path and os.path.exists(path) else None
    return _tools[name]


def need(name: str) -> str:
    path = tool(name)
    if not path:
        raise ToolMissing(
            f"{name} is not installed on this server. Deploy with the Dockerfile (it installs ffmpeg), "
            "install ffmpeg, or add `imageio-ffmpeg` to requirements.txt."
        )
    return path


def tools_status() -> dict:
    ff, fp = tool("ffmpeg"), tool("ffprobe")
    bundled = bool(ff and not shutil.which("ffmpeg"))
    return {"ffmpeg": ff, "ffprobe": fp, "bundled": bundled}


async def _run(*cmd) -> tuple[int, str, str]:
    cmd = (need(cmd[0]) if cmd[0] in ("ffmpeg", "ffprobe") else cmd[0], *cmd[1:])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise ToolMissing(f"{os.path.basename(cmd[0])} could not be started ({exc.strerror or 'not found'}).") from None
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:  # the job was cancelled: do not leave ffmpeg running
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


async def probe(path: str) -> dict:
    """duration (s), width, height and the audio codec via ffprobe (or `ffmpeg -i` when there is no ffprobe).
    `ok` is False if neither could read the file - callers treat that as "unknown", never as an error."""
    info = {"duration": 0, "width": 0, "height": 0, "audio": "", "ok": False}
    try:
        if tool("ffprobe"):
            rc, out, _ = await _run(
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,channels:format=duration",
                "-of", "json", path,
            )
            if rc == 0:
                data = json.loads(out)
                streams = data.get("streams") or []
                v = next((x for x in streams if x.get("codec_type") == "video"), {})
                au = next((x for x in streams if x.get("codec_type") == "audio"), {})
                info["width"], info["height"] = int(v.get("width") or 0), int(v.get("height") or 0)
                info["duration"] = int(float((data.get("format") or {}).get("duration") or 0))
                info["audio"] = au.get("codec_name", "") if au else ""
                info["ok"] = bool(v)
        elif tool("ffmpeg"):
            _, _, err = await _run("ffmpeg", "-hide_banner", "-i", path)  # exits 1 (no output file) but prints the streams
            d = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
            v = re.search(r"Video:.*?[\s,](\d{2,5})x(\d{2,5})", err)
            au = re.search(r"Audio:\s*([\w]+)", err)
            if d:
                info["duration"] = int(int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)))
            if v:
                info["width"], info["height"] = int(v.group(1)), int(v.group(2))
            info["audio"] = au.group(1) if au else ""
            info["ok"] = bool(v)
    except (ToolMissing, ValueError, OSError):
        pass
    return info


async def add_silent_audio(path: str) -> bool:
    """Telegram shows a video WITHOUT an audio track as a GIF (silent, looping). Add a silent AAC track so
    it stays a normal video. Returns True if the file was replaced."""
    tmp = path + ".silent.mp4"
    if not tool("ffmpeg"):
        return False
    rc, _, _ = await _run(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "32k", "-shortest", "-movflags", "+faststart", tmp,
    )
    if rc == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, path)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


async def make_thumb(path: str, out: str, at: int = 5) -> str | None:
    if not tool("ffmpeg"):
        return None  # a thumbnail is optional
    rc, _, _ = await _run(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at), "-i", path,
        "-frames:v", "1", "-vf", "scale=320:-1", out,
    )
    if rc != 0 or not os.path.exists(out):
        rc, _, _ = await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path, "-frames:v", "1", "-vf", "scale=320:-1", out)
    return out if rc == 0 and os.path.exists(out) else None


async def split_if_needed(path: str, max_bytes: int) -> list[str]:
    """Split by duration (stream copy) so each part fits under max_bytes.
    Cuts land on keyframes, so parts are uneven; if one is still too big the
    split is retried with shorter segments. The original is kept until a split works."""
    size = os.path.getsize(path)
    if size <= max_bytes:
        return [path]
    need("ffmpeg")  # splitting a big file cannot be done without it
    info = await probe(path)
    if not info["duration"]:
        raise RuntimeError("File is too big for Telegram and its duration is unknown, cannot split.")

    base, _ = os.path.splitext(path)
    dirname = os.path.dirname(path) or "."
    prefix = os.path.basename(base) + "_part"
    parts_needed = math.ceil(size / (max_bytes * 0.92))
    seg = max(1, math.ceil(info["duration"] / parts_needed))

    for _attempt in range(4):
        for f in os.listdir(dirname):  # clear leftovers from a failed attempt
            if f.startswith(prefix) and f.endswith(".mp4"):
                os.remove(os.path.join(dirname, f))
        rc, _, err = await _run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path, "-map", "0", "-c", "copy",
            "-f", "segment", "-segment_time", str(seg),
            "-reset_timestamps", "1", "-segment_format", "mp4", f"{base}_part%02d.mp4",
        )
        if rc != 0:
            raise RuntimeError(f"ffmpeg split failed: {err[-300:]}")
        files = sorted(
            os.path.join(dirname, f) for f in os.listdir(dirname)
            if f.startswith(prefix) and f.endswith(".mp4")
        )
        if files and all(os.path.getsize(f) <= max_bytes for f in files):
            os.remove(path)
            return files
        seg = max(1, int(seg * 0.6))
    raise RuntimeError("Could not split the video under the Telegram size limit. Lower MAX_HEIGHT.")


# ------------------------------------------------------------- download
def _ytdlp_download(url: str, headers: dict, outtmpl: str, progress, check=None) -> str:
    from yt_dlp import YoutubeDL

    mh = config.MAX_HEIGHT

    def hook(d):
        if check:
            check()
        if progress and d.get("status") == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if not total and d.get("fragment_count"):
                pct = 100 * (d.get("fragment_index") or 0) / d["fragment_count"]
            else:
                pct = (100 * done / total) if total else 0
            progress({"pct": pct, "speed": d.get("speed") or 0, "eta": d.get("eta") or 0, "done": done})

    opts = {
        "format": f"bv*[height<=?{mh}]+ba/b[height<=?{mh}]/b",
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "http_headers": headers,
        "concurrent_fragment_downloads": 4,
        **({"proxy": config.PROXY} if config.PROXY else {}),
        **({"ffmpeg_location": tool("ffmpeg")} if tool("ffmpeg") else {}),
        "retries": 5,
        "fragment_retries": 10,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        reqs = info.get("requested_downloads") or []
        if reqs and reqs[0].get("filepath"):
            return reqs[0]["filepath"]
        return ydl.prepare_filename(info)


def _short_err(text: str, n: int = 3) -> str:
    """The last few meaningful lines of a tool's error output (no banners, no ANSI)."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    pick = [ln for ln in lines if re.search(r"error|invalid|not found|denied|forbidden|failed|40\d|50\d", ln, re.I)]
    return " | ".join((pick or lines)[-n:])[:300]


async def _ffmpeg_download(url: str, headers: dict, out: str) -> str:
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-user_agent", headers.get("User-Agent", UA)]
    if config.PROXY and config.PROXY.lower().startswith("http"):
        base += ["-http_proxy", config.PROXY]
    base += ["-headers", hdr]
    # HLS segments are sometimes named .jpeg/.png/.txt; ffmpeg refuses those unless allowed.
    # Older ffmpeg builds do not know every option, so fall back to fewer flags.
    attempts = [["-allowed_extensions", "ALL", "-extension_picky", "0"], ["-allowed_extensions", "ALL"], []]
    err = ""
    for extra in attempts:
        rc, _, err = await _run(*base, *extra, "-i", url, "-c", "copy", out)
        if rc == 0:
            return out
        if not re.search(r"option .*not found|unrecognized option", err, re.I):
            break
    raise RuntimeError(_short_err(err))


# ------------------------------------------------------------- native HLS downloader
# Plain-Python HLS client: fetches the playlist and every segment itself with exactly the headers we
# give it, so it does not depend on yt-dlp/ffmpeg accepting the URL, the segment extension
# (.jpeg/.png/.txt) or their header handling.
#   * one kept-alive HTTP(S) connection per worker thread (no TLS handshake per segment), proxy aware
#   * adaptive concurrency: backs off when the host answers 5xx/429/timeouts, speeds up again on success
#   * patient retries (exponential backoff + jitter) and per-segment failover to the page's other mirrors
#   * segments are written to disk in order as they arrive (bounded memory, no second pass)
#   * master playlists, AES-128, fMP4 (EXT-X-MAP), and segments with a fake image header in front
class _HLSError(RuntimeError):
    def __init__(self, msg: str = "", blocked: bool = False):
        super().__init__(msg)
        self.blocked = blocked  # the host answered 403 / 429: waiting and trying again later may work


class _SegmentError(_HLSError):
    """A media segment could not be fetched from any mirror. yt-dlp/ffmpeg would hit the same wall."""


class _HTTPStatus(Exception):
    def __init__(self, code: int, retry_after: float = 0):
        super().__init__(f"HTTP {code}")
        self.code, self.retry_after = code, retry_after


def _attrs(line: str) -> dict:
    body = line.split(":", 1)[1] if ":" in line else ""
    return {k.upper(): v.strip('"') for k, v in re.findall(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]*)', body)}


class _Net:
    """Thread-safe fetcher for many small requests: keep-alive connection per thread, redirects, proxy(ies),
    a small cookie jar (CDN cookies such as __cf_bm must come back on the next request), and header
    *profiles*: `get(url, tier=n)` sends the n-th variant of the headers, so a worker that is answered 403 can
    try the next one. The last tier is a real-browser TLS fingerprint through `curl_cffi`, when it is installed."""

    def __init__(self, headers: dict, proxy="", timeout: float = 20, ctx=None, profiles: list | None = None):
        # a Cookie header (the cookies a browser had for the stream's site) is only ever sent to the host of the
        # first request and to `cookie_hosts` (mirrors): never to a host a redirect sends us to
        self.static_cookie = next((v for k, v in headers.items() if k.lower() == "cookie"), "")
        self.cookie_hosts: set[str] | None = None
        self.headers = {**{k: v for k, v in headers.items() if k.lower() != "cookie"},
                        "Accept-Encoding": "identity", "Connection": "keep-alive"}
        self.profiles = profiles or [{}]
        self.timeout, self._ctx = timeout, ctx
        plist = [proxy] if isinstance(proxy, str) and proxy else list(proxy or [])
        self.proxies = [urlsplit(p) for p in plist]
        if any(p.scheme.lower().startswith("socks") for p in self.proxies):
            raise _HLSError("socks proxy is not supported by the built-in downloader")
        self.pi = 0
        self.lock = threading.Lock()
        self.jar: dict[str, dict[str, str]] = {}
        self.local = threading.local()

    # -- proxies
    @property
    def proxy(self):
        return self.proxies[self.pi % len(self.proxies)] if self.proxies else None

    def rotate_proxy(self) -> bool:
        """Switch every worker to the next proxy (a blocked IP). False when there is only one (or none)."""
        if len(self.proxies) < 2:
            return False
        with self.lock:
            self.pi = (self.pi + 1) % len(self.proxies)
        return True

    # -- tiers
    def n_tiers(self) -> int:
        return len(self.profiles) + (1 if _cffi() else 0)

    def _hdrs(self, tier: int, extra: dict | None, host: str) -> dict:
        h = dict(self.headers)
        for k, v in self.profiles[min(tier, len(self.profiles) - 1)].items():
            if v is None:
                h.pop(k, None)
            else:
                h[k] = v
        if extra:
            h.update(extra)
        with self.lock:
            ck = "; ".join(f"{k}={v}" for k, v in self.jar.get(host, {}).items())
            if self.static_cookie:
                if self.cookie_hosts is None:
                    self.cookie_hosts = {host}
                if host in self.cookie_hosts:
                    ck = self.static_cookie + ("; " + ck if ck else "")
        if ck:
            h["Cookie"] = ck
        return h

    def _remember(self, host: str, resp) -> None:
        try:
            cookies = resp.msg.get_all("Set-Cookie") or []
        except Exception:  # noqa: BLE001
            return
        for sc in cookies:
            name, _, val = sc.split(";", 1)[0].partition("=")
            name = name.strip()
            if name and re.fullmatch(r"[\w.\-%]+", name):
                with self.lock:
                    self.jar.setdefault(host, {})[name] = val.strip()

    # -- connections
    def _pool(self) -> dict:
        if not hasattr(self.local, "pool"):
            self.local.pool = {}
        return self.local.pool

    def _drop(self, key):
        conn = self._pool().pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        for key in list(self._pool()):
            self._drop(key)

    def _connect(self, scheme: str, host: str, port: int):
        ctx = self._ctx or ssl.create_default_context()
        extra: dict = {}
        proxy = self.proxy
        if proxy:
            ph, pp = proxy.hostname, proxy.port or 8080
            if proxy.username:
                cred = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}".encode()
                extra["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode()
            if scheme == "https":  # CONNECT tunnel through the proxy, TLS to the real host inside it
                conn = http.client.HTTPSConnection(ph, pp, timeout=self.timeout, context=ctx)
                conn.set_tunnel(host, port, headers=extra)
                extra = {}
            else:
                conn = http.client.HTTPConnection(ph, pp, timeout=self.timeout)
        elif scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        conn._extra_headers = extra  # noqa: SLF001
        return conn

    def _open(self, url: str, extra: dict | None = None, tier: int = 0):
        """Send a GET (following redirects) and return (response, pool_key). The body is NOT read."""
        for _hop in range(6):
            s = urlsplit(url)
            scheme, host = s.scheme.lower(), s.hostname
            if scheme not in ("http", "https") or not host:
                raise _HLSError("bad url")
            port = s.port or (443 if scheme == "https" else 80)
            pidx = self.pi if self.proxies else 0
            key = (scheme, host, port, pidx)
            target = (s.path or "/") + (f"?{s.query}" if s.query else "")
            if self.proxy and scheme == "http":
                target = url  # absolute-form request for a plain-http proxy
            hdrs = self._hdrs(tier, extra, host)
            while True:
                conn = self._pool().get(key)
                reused = conn is not None
                if conn is None:
                    conn = self._pool()[key] = self._connect(scheme, host, port)
                try:
                    conn.request("GET", target, headers={**hdrs, **conn._extra_headers})  # noqa: SLF001
                    resp = conn.getresponse()
                except (http.client.HTTPException, OSError):
                    self._drop(key)
                    if reused:  # a kept-alive connection the server already closed: retry once on a new one
                        continue
                    raise
                break
            self._remember(host, resp)
            loc = resp.getheader("Location")
            if resp.status in (301, 302, 303, 307, 308) and loc:
                try:
                    resp.read()
                except (http.client.HTTPException, OSError):
                    self._drop(key)
                url = urljoin(url, loc)
                continue
            return resp, key
        raise _HLSError("too many redirects")

    def _cffi_get(self, url: str, extra: dict | None) -> bytes:
        """Last tier: Chrome's TLS/HTTP2 fingerprint (needs `pip install curl_cffi`). Some CDNs answer 403 to
        Python's TLS handshake whatever the headers are."""
        from curl_cffi import requests as cr  # noqa: PLC0415
        sess = getattr(self.local, "cffi", None)
        if sess is None:
            sess = self.local.cffi = cr.Session(impersonate=os.getenv("IMPERSONATE", "chrome"))
        prof = self.profiles[min(1, len(self.profiles) - 1)]  # Referer/Origin + browser headers; UA comes from the impersonation
        hdrs = {k: v for k, v in {**self.headers, **prof, **(extra or {})}.items()
                if v is not None and k.lower() not in ("accept-encoding", "connection", "user-agent")}
        proxy = self.proxy.geturl() if self.proxy else None
        try:
            r = sess.get(url, headers=hdrs, timeout=self.timeout, allow_redirects=True,
                         proxies={"http": proxy, "https": proxy} if proxy else None)
        except Exception as exc:  # noqa: BLE001  (curl_cffi has its own exception classes)
            raise OSError(type(exc).__name__) from None
        if r.status_code in (200, 206):
            return r.content
        raise _HTTPStatus(r.status_code, _retry_after(r.headers.get("Retry-After")))

    def get(self, url: str, extra: dict | None = None, tier: int = 0) -> bytes:
        """GET `url` -> body. Raises _HTTPStatus for non-200/206, OSError/HTTPException for network trouble."""
        if tier >= len(self.profiles):
            return self._cffi_get(url, extra)
        resp, key = self._open(url, extra, tier)
        try:
            body = resp.read()
        except (http.client.HTTPException, OSError):
            self._drop(key)
            raise
        if resp.status in (200, 206):
            return body
        raise _HTTPStatus(resp.status, _retry_after(resp.getheader("Retry-After")))

    def probe(self, url: str, extra: dict | None = None, limit: int = 4096) -> tuple[int, dict, bytes]:
        """Start a GET and read at most `limit` bytes of the body -> (status, headers, first bytes).
        Never downloads a whole file, even if the server ignores a Range header."""
        resp, key = self._open(url, extra)
        try:
            data = resp.read(limit)
        except (http.client.HTTPException, OSError):
            self._drop(key)
            raise
        headers = {k.lower(): v for k, v in resp.getheaders()}
        if len(data) >= limit or not resp.isclosed():
            self._drop(key)  # part of the body is unread: this connection cannot be reused
        return resp.status, headers, data

    def stream(self, url: str, extra: dict | None = None, chunk: int = 1 << 20):
        """GET and yield the body in pieces; the first item is (status, headers), then bytes."""
        resp, key = self._open(url, extra)
        yield resp.status, {k.lower(): v for k, v in resp.getheaders()}
        if resp.status not in (200, 206):
            try:
                resp.read()
            except (http.client.HTTPException, OSError):
                self._drop(key)
            return
        try:
            while True:
                data = resp.read(chunk)
                if not data:
                    break
                yield data
        except (http.client.HTTPException, OSError):
            self._drop(key)
            raise
        except GeneratorExit:  # the consumer stopped early: the connection still has unread data
            self._drop(key)
            raise


_cffi_ok: bool | None = None


def _cffi() -> bool:
    global _cffi_ok
    if _cffi_ok is None:
        try:
            import curl_cffi  # noqa: F401, PLC0415
            _cffi_ok = True
        except Exception:  # noqa: BLE001
            _cffi_ok = False
    return _cffi_ok


def _header_profiles(headers: dict) -> list[dict]:
    """Header variants for a stream host that answers 403: each dict overrides the base headers (None = remove).
    0 = exactly what the playlist check used (known to work there); then the full browser set with Origin;
    the player site's root as Referer; the configured player referer; and finally no Referer at all."""
    ref = headers.get("Referer", "")
    origin, root = _origin(ref), ""
    root = origin + "/" if origin else ""
    out: list[dict] = [{}]
    if origin:
        out.append({**_BROWSER_HEADERS, "Origin": origin})
    if root and root != ref:
        out.append({**_BROWSER_HEADERS, "Referer": root, "Origin": origin})
    cfg = config.M3U8_REFERER
    if cfg and cfg not in (ref, root):
        out.append({**_BROWSER_HEADERS, "Referer": cfg, "Origin": _origin(cfg)})
    out.append({"Referer": None, "Origin": None})
    uniq: list[dict] = []
    for p in out:
        if p not in uniq:
            uniq.append(p)
    return uniq


class _Gate:
    """Adaptive concurrency limit: starts careful, creeps up while the host is happy, halves when it struggles."""

    def __init__(self, maximum: int, minimum: int = 2, start: int | None = None):
        self.max, self.min = maximum, min(minimum, maximum)
        self.limit = max(self.min, min(maximum, start or maximum))
        self.active, self.streak, self.last_cut = 0, 0, 0.0
        self.cv = threading.Condition()

    def __enter__(self):
        with self.cv:
            while self.active >= self.limit:
                self.cv.wait(0.5)
            self.active += 1

    def __exit__(self, *exc):
        with self.cv:
            self.active -= 1
            self.cv.notify_all()

    def bad(self, hard: bool = False) -> bool:
        """Report a throttling-type failure. True if this lowered the concurrency limit.
        `hard` (HTTP 429) may go all the way down to one request at a time."""
        with self.cv:
            now = time.monotonic()
            self.streak = 0
            floor = 1 if hard else self.min
            if now - self.last_cut > GATE_COOLDOWN and self.limit > floor:
                self.limit = max(floor, self.limit // 2)
                self.last_cut = now
                return True
            return False

    def good(self):
        with self.cv:
            self.streak += 1
            if self.streak >= 12 and self.limit < self.max:
                self.limit += 1
                self.streak = 0
                self.cv.notify_all()


class _Pacer:
    """Spaces the START of requests (all workers together): a CDN rule that counts requests per second sees a
    steady trickle instead of bursts. The gap doubles after a 429 and relaxes again while things are calm."""

    def __init__(self, base: float):
        self.base, self.interval, self.next, self.ok, self.last_cut = base, base, 0.0, 0, 0.0
        self.lock = threading.Lock()

    def slot(self) -> float:
        """Seconds this request has to wait before it may start."""
        if self.interval <= 0:
            return 0.0
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next)
            self.next = at + self.interval * random.uniform(0.7, 1.3)
            return at - now

    def slower(self):
        """Double the gap (at most once every 2 s: several workers get the same 429 together)."""
        with self.lock:
            now = time.monotonic()
            if now - self.last_cut < 2.0:
                return
            self.last_cut = now
            self.interval = min(max(self.interval * 2, 0.25), 4.0)
            self.ok = 0

    def good(self):
        with self.lock:
            self.ok += 1
            if self.ok >= 40 and self.interval > self.base:
                self.interval = max(self.base, self.interval * 0.8)
                self.ok = 0


class _Breaker:
    """After a 429 EVERY worker waits (the host asked us to slow down), not only the one that got the answer."""

    def __init__(self):
        self.until = 0.0
        self.lock = threading.Lock()

    def trip(self, seconds: float):
        with self.lock:
            self.until = max(self.until, time.monotonic() + seconds)

    def left(self) -> float:
        return max(0.0, self.until - time.monotonic())


MAX_ATTEMPTS = 8
GATE_COOLDOWN = 1.5  # seconds between two concurrency cuts
FREE_RETRIES = 3     # failures that lowered the concurrency do not use up a segment's retries (bounded)


def _backoff(attempt: int, retry_after: float = 0) -> float:
    """1.5s, 3s, 6s, 12s, 20s ... with jitter, never less than the server's Retry-After."""
    return max(retry_after, min(1.5 * (2 ** attempt), 20)) * random.uniform(0.7, 1.3)


def _rank_variants_ex(text: str, base: str, max_h: int) -> list[dict]:
    """Media playlists of a master, best first (up to max_h), then the lower/higher ones as fallbacks.
    Each: {"url", "height", "bw", "audio": group id of its separate audio rendition or ''}"""
    lines = text.splitlines()
    variants = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        a = _attrs(line)
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
            j += 1
        if j >= len(lines):
            continue
        res = a.get("RESOLUTION", "")
        height = int(res.split("x")[1]) if "x" in res and res.split("x")[1].isdigit() else 0
        try:
            bw = int(float(a.get("AVERAGE-BANDWIDTH") or a.get("BANDWIDTH") or 0))
        except ValueError:
            bw = 0
        variants.append({"url": urljoin(base, lines[j].strip()), "height": height, "bw": bw, "audio": a.get("AUDIO", "")})
    key = lambda v: (v["height"], v["bw"])  # noqa: E731
    fits = sorted((v for v in variants if not max_h or not v["height"] or v["height"] <= max_h), key=key, reverse=True)
    rest = sorted((v for v in variants if v not in fits), key=key)
    return fits + rest


def _rank_variants(text: str, base: str, max_h: int) -> list[str]:
    return [v["url"] for v in _rank_variants_ex(text, base, max_h)]


def _audio_renditions(text: str, base: str) -> dict[str, list[dict]]:
    """#EXT-X-MEDIA:TYPE=AUDIO entries by GROUP-ID: demuxed streams keep the sound in a playlist of its own
    that the video variant only points to (AUDIO="group"). Ignoring it gives a silent video."""
    out: dict[str, list[dict]] = {}
    for line in text.splitlines():
        if not line.startswith("#EXT-X-MEDIA"):
            continue
        a = _attrs(line)
        if a.get("TYPE", "").upper() != "AUDIO" or not a.get("URI"):
            continue
        out.setdefault(a.get("GROUP-ID", ""), []).append({
            "uri": urljoin(base, a["URI"]), "default": a.get("DEFAULT", "").upper() == "YES",
            "name": a.get("NAME", ""), "lang": a.get("LANGUAGE", ""),
        })
    return out


def pick_audio(text: str, base: str, group: str) -> dict | None:
    """The audio rendition to use for a variant: the DEFAULT one of its group (else the first)."""
    opts = _audio_renditions(text, base).get(group) or []
    return next((o for o in opts if o["default"]), opts[0] if opts else None)


def _parse_media(text: str, base: str):
    segs, key, init, seq = [], None, None, 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1].strip() or 0)
        elif line.startswith("#EXT-X-BYTERANGE"):
            raise _HLSError("byte-range segments are not supported by the built-in downloader")
        elif line.startswith("#EXT-X-KEY"):
            a = _attrs(line)
            method = a.get("METHOD", "NONE").upper()
            if method == "NONE":
                key = None
            elif method == "AES-128" and a.get("URI"):
                key = {"uri": urljoin(base, a["URI"]), "iv": a.get("IV")}
            else:
                raise _HLSError(f"unsupported encryption {method}")
        elif line.startswith("#EXT-X-MAP"):
            a = _attrs(line)
            if a.get("URI"):
                init = urljoin(base, a["URI"])
        elif line.startswith("#"):
            continue
        else:
            segs.append((urljoin(base, line), key, seq))
            seq += 1
    return segs, init


def _strip_ts_prefix(data: bytes) -> bytes:
    """Some hosts put a fake PNG/GIF/JPEG header in front of each MPEG-TS segment.
    Cut to the first real TS packet (0x47 sync byte repeated every 188 bytes)."""
    n = len(data)
    if n > 376 and data[0] == 0x47 and data[188] == 0x47:
        return data
    for i in range(min(n - 376, 262_144)):
        if data[i] == 0x47 and data[i + 188] == 0x47 and data[i + 376] == 0x47:
            return data[i:]
    return data


def _decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise _HLSError("AES-128 stream needs `pip install cryptography`") from None
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = dec.update(data) + dec.finalize()
    pad = out[-1] if out else 0
    return out[:-pad] if 1 <= pad <= 16 else out


def playable(url: str, headers: dict) -> bool:
    """Quick check that `url` answers with a real playlist using these headers."""
    try:
        net = _Net(headers, config.PROXY, timeout=10)
        try:
            data = net.get(url)
        finally:
            net.close()
    except Exception:  # noqa: BLE001
        return False
    return data.decode("utf-8", "ignore").lstrip("\ufeff \r\n\t").startswith("#EXTM3U")


def _noop():
    return None


def _native_download(
    url: str, headers: dict, out_mp4: str, progress=None, mirrors=(), check=None,
    workers: int | None = None, ctx=None, resume: dict | None = None, profiles: list | None = None,
) -> str:
    """Download an HLS stream to `out_mp4`.

    Built for a host that blocks: HTTP 403 rotates the header profile (and the proxy, when several are set),
    HTTP 429 pauses ALL workers for the time the host asks and slows the request rate down. If the host still
    refuses, the error says so (`blocked`) and, with a `resume` dict, the next call continues after the last
    segment that was written instead of starting again."""
    check = check or _noop
    workers = max(1, workers or config.DL_WORKERS)
    net = _Net(headers, config.PROXIES or config.PROXY, ctx=ctx, profiles=profiles or _header_profiles(headers))
    gate = _Gate(workers, start=min(workers, 3))
    pacer = _Pacer(config.DL_MIN_INTERVAL_MS / 1000)
    breaker = _Breaker()
    ntiers = net.n_tiers()
    shared = {"tier": 0, "good": 0}
    shared_lock = threading.Lock()
    hosts = [urlsplit(u) for u in [url, *mirrors]]  # same stream on other hosts (same path)
    host_set = {h.netloc for h in hosts}
    net.cookie_hosts = {h.hostname for h in hosts if h.hostname}
    pref = {"i": 0}
    last_ok = {"host": urlsplit(url).netloc}
    rs: dict = resume if resume is not None else {}

    def variants(u: str) -> list[str]:
        s = urlsplit(u)
        if s.netloc not in host_set:
            return [u]
        return [u] + [s._replace(scheme=h.scheme, netloc=h.netloc).geturl() for h in hosts if h.netloc != s.netloc]

    def sleep(seconds: float):
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            check()
            time.sleep(min(0.4, left))

    def advance(tier: int) -> bool:
        """The header profile `tier` was refused: move everybody to the next one. True if there is one to try."""
        with shared_lock:
            if shared["tier"] != tier:  # another worker already moved on: just use that one
                return True
            if tier + 1 < ntiers:
                shared["tier"] = tier + 1
                log.info("403: trying header profile %d of %d", tier + 2, ntiers)
                return True
            return False

    def fetch(u: str, what: str, attempts: int = MAX_ATTEMPTS) -> bytes:
        """GET with retries and failover across mirrors. Raises _SegmentError for media, _HLSError otherwise."""
        urls, dead, last = variants(u), set(), "error"
        attempt, free, blocks = 0, FREE_RETRIES, 0
        forbidden: dict[str, int] = {}
        while attempt < attempts and blocks <= config.BLOCK_ATTEMPTS:
            check()
            cand = [x for x in urls if x not in dead]
            if not cand:
                break
            order = cand[pref["i"] % len(cand):] + cand[:pref["i"] % len(cand)]
            cur = order[attempt % len(order)]
            # everybody waits while the host asked us to slow down; requests start at a steady pace
            if (wait := breaker.left()) > 0:
                sleep(wait + random.uniform(0, 0.5))
            if (gap := pacer.slot()) > 0:
                sleep(gap)
            retry_after, cut = 0.0, False
            tier = shared["tier"]
            try:
                with gate:
                    data = net.get(cur, tier=tier)
                if not data:
                    raise OSError("empty body")
                gate.good()
                pacer.good()
                shared["good"] = tier
                last_ok["host"] = urlsplit(cur).netloc
                if len(urls) > 1:
                    pref["i"] = urls.index(cur)
                return data
            except _HTTPStatus as exc:
                last = f"HTTP {exc.code}"
                if exc.code == 429:  # slow down: all workers pause, one request at a time, bigger gaps
                    blocks += 1
                    # the host's own Retry-After when it gives one, else 4s, 8s, 16s... (a little longer each time)
                    pause = (exc.retry_after or min(2.0 * 2 ** min(blocks, 5), 60.0)) * (1 + 0.15 * min(blocks - 1, 8)) * random.uniform(1.0, 1.25)
                    breaker.trip(pause)
                    pacer.slower()
                    gate.bad(hard=True)
                    if blocks % 4 == 0 and net.rotate_proxy():
                        log.info("429: switched to the next proxy")
                    continue
                if exc.code == 403:  # refused: another header profile, then another proxy, then wait
                    blocks += 1
                    if advance(tier):
                        continue
                    if net.rotate_proxy():
                        with shared_lock:
                            shared["tier"] = 0
                        log.info("403 with every header profile: switched to the next proxy")
                        continue
                    forbidden[cur] = forbidden.get(cur, 0) + 1
                    if forbidden[cur] >= 2:  # refused twice after trying everything: this mirror will not have it
                        dead.add(cur)
                        attempt += 1
                        continue
                    breaker.trip(random.uniform(8, 15))
                    with shared_lock:
                        shared["tier"] = shared["good"]  # start the sweep again from what worked last
                    continue
                if exc.code in (400, 401, 404, 410):
                    dead.add(cur)  # this mirror will not have it: try the next one right away
                    attempt += 1
                    continue
                cut = gate.bad()
                retry_after = exc.retry_after
            except (http.client.HTTPException, OSError) as exc:
                last = type(exc).__name__
                cut = gate.bad()
            if cut and free > 0:
                free -= 1  # we just slowed down: this failure was our fault, not the segment's
            else:
                attempt += 1
            if attempt < attempts:
                sleep(_backoff(attempt, retry_after))
        blocked = last in ("HTTP 403", "HTTP 429")
        err = _SegmentError if (what in ("segment", "audio segment") or blocked) else _HLSError
        raise err(f"{urlsplit(u).netloc}: {what} failed - {last}", blocked)

    def playlist(u: str) -> tuple[str, str]:
        data = fetch(u, "playlist", attempts=5).decode("utf-8", "ignore")
        if not data.lstrip("\ufeff \r\n\t").startswith("#EXTM3U"):
            raise _HLSError(f"{urlsplit(u).netloc}: not a playlist")
        return data, u

    t0 = time.monotonic()
    totals = {"n": 0, "n_done": 0, "bytes": 0, "emit": 0.0}  # across all tracks (video + audio)
    samples: list[tuple[float, int]] = [(t0, 0)]

    def emit(st: dict, force: bool = False, stage: str = "download"):
        if not progress:
            return
        now = time.monotonic()
        if not force and now - totals["emit"] < 0.5:
            return
        totals["emit"] = now
        n_done, got = totals["n_done"] + st["fetched"], totals["bytes"] + st["bytes"]
        samples.append((now, got))
        while len(samples) > 2 and now - samples[0][0] > 10:
            samples.pop(0)
        dt = max(now - samples[0][0], 0.001)
        speed = (got - samples[0][1]) / dt if dt >= 1 else (got - samples[0][1]) / max(now - t0, 0.001)
        est_total = got / max(n_done, 1) * totals["n"]
        eta = max(est_total - got, 0) / speed if speed > 0 else 0
        progress({
            "pct": 100 * n_done / max(totals["n"], 1), "speed": max(speed, 0), "eta": eta, "done": got,
            "seg_done": n_done, "seg_total": totals["n"], "workers": gate.limit,
            "host": last_ok["host"], "stage": stage,
        })

    def run_track(segs, init, joined_path: str, what: str, track: str) -> None:
        """Download one track (video or audio): workers fetch in order, results are written in order.
        With `resume`, a track that was interrupted continues after the last segment that reached the disk."""
        total = len(segs)
        start = int(rs.get(track, 0))
        keys: dict[str, bytes] = {}
        cv = threading.Condition()
        st = {"next_task": start, "next_write": start, "fetched": start, "bytes": int(rs.get(track + "_size", 0)) if start else 0, "err": None}
        buf: dict[int, bytes] = {}
        window = workers * config.DL_WINDOW  # how far ahead of the write position workers may run (bounds memory)
        out = open(joined_path, "ab" if start else "wb")

        def process(i: int, data: bytes) -> bytes:
            _, key, seq = segs[i]
            if key:
                with cv:
                    kb = keys.get(key["uri"])
                if kb is None:
                    kb = fetch(key["uri"], "key")
                    with cv:
                        keys[key["uri"]] = kb
                iv = bytes.fromhex(key["iv"][2:].zfill(32)) if key["iv"] else seq.to_bytes(16, "big")
                data = _decrypt(data, kb, iv)
            return data if init else _strip_ts_prefix(data)

        def worker():
            try:
                while True:
                    with cv:
                        if st["err"] or st["next_task"] >= total:
                            return
                        i = st["next_task"]
                        st["next_task"] += 1
                        while i - st["next_write"] >= window and not st["err"]:
                            cv.wait(0.5)
                            check()
                        if st["err"]:
                            return
                    data = process(i, fetch(segs[i][0], what))
                    with cv:
                        buf[i] = data
                        st["fetched"] += 1
                        st["bytes"] += len(data)
                        while st["next_write"] in buf:
                            out.write(buf.pop(st["next_write"]))
                            st["next_write"] += 1
                        cv.notify_all()
                    emit(st)
            except BaseException as exc:  # noqa: BLE001  (incl. JobCancelled): stop everyone, re-raise in the caller
                with cv:
                    st["err"] = st["err"] or exc
                    cv.notify_all()

        try:
            if init and not start:
                out.write(fetch(init, "init segment"))
            threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(workers, max(total - start, 0)))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            out.close()
            # what is on disk now: the next attempt continues from here (segments are written strictly in order)
            rs[track] = st["next_write"]
            rs[track + "_size"] = os.path.getsize(joined_path) if os.path.exists(joined_path) else 0
        if st["err"]:
            raise st["err"]
        totals["n_done"] += st["fetched"]
        totals["bytes"] += st["bytes"]

    joined = out_mp4 + ".joined"
    joined_audio = out_mp4 + ".audio.joined"
    done = False
    try:
        text, base = playlist(url)
        audio_url = ""
        if "#EXT-X-STREAM-INF" in text:
            master, err = text, None
            for v in _rank_variants_ex(master, base, config.MAX_HEIGHT)[:3]:
                try:
                    text, base = playlist(v["url"])
                    pa = pick_audio(master, url, v["audio"]) if v["audio"] else None
                    audio_url = pa["uri"] if pa and pa["uri"] != v["url"] else ""
                    break
                except _HLSError as exc:
                    err = exc
            else:
                raise err or _HLSError("master playlist has no variants")
        segs, init = _parse_media(text, base)
        if not segs:
            raise _HLSError("playlist has no segments")
        a_segs, a_init = [], None
        if audio_url:  # demuxed stream: the sound lives in its own playlist
            a_text, a_base = playlist(audio_url)
            a_segs, a_init = _parse_media(a_text, a_base)
        totals["n"] = len(segs) + len(a_segs)

        # continue an interrupted download only when it is the same stream and the files are still as we left them
        rkey = [urlsplit(url).path, len(segs), len(a_segs)]
        intact = (
            rs.get("key") == rkey
            and all(not rs.get(t) or (os.path.exists(p) and os.path.getsize(p) == rs.get(t + "_size"))
                    for t, p in (("v", joined), ("a", joined_audio)))
        )
        if not intact:
            rs.clear()
            for leftover in (joined, joined_audio):
                if os.path.exists(leftover):
                    os.remove(leftover)
        rs.update(key=rkey)
        if rs.get("v"):
            samples[0] = (t0, rs.get("v_size", 0))
            log.info("continuing after segment %d of %d", rs["v"], len(segs))

        run_track(segs, init, joined, "segment", "v")
        if a_segs:
            run_track(a_segs, a_init, joined_audio, "audio segment", "a")
        emit({"fetched": 0, "bytes": 0}, force=True, stage="finalize")

        # ---- remux into a clean mp4 (video + the separate audio, if any); cancellable
        cmd = [need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts", "-i", joined]
        if a_segs:
            cmd += ["-i", joined_audio, "-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-c", "copy", "-movflags", "+faststart", out_mp4]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError:
            raise ToolMissing("ffmpeg could not be started.") from None
        try:
            while True:
                try:
                    _, err_text = proc.communicate(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    check()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if proc.returncode != 0 or not os.path.exists(out_mp4):
            raise _HLSError("ffmpeg remux failed: " + _short_err(err_text))
        done = True
        return out_mp4
    finally:
        net.close()
        # keep the partial files when the caller wants to continue (resume) after a failure
        if done or resume is None:
            for leftover in (joined, joined_audio):
                if os.path.exists(leftover):
                    os.remove(leftover)
            rs.clear()


# ------------------------------------------------------------- direct video files (progressive mp4/mkv/webm)
def _looks_like_media(headers: dict, data: bytes) -> bool:
    if data[4:8] == b"ftyp" or data[:4] == b"\x1a\x45\xdf\xa3":  # mp4/mov, or matroska/webm
        return True
    return headers.get("content-type", "").lower().startswith("video/") and len(data) > 0


def _total_size(status: int, headers: dict) -> int:
    m = re.search(r"/(\d+)\s*$", headers.get("content-range", ""))
    if status == 206 and m:
        return int(m.group(1))
    return int(headers.get("content-length", 0) or 0) if status == 200 else 0


def probe_file(url: str, headers: dict) -> tuple[bool, int, bool, int]:
    """-> (is a video file, total size in bytes (0 = unknown), server supports Range, HTTP status).
    Reads only a few bytes, never the whole file."""
    net = _Net(headers, config.PROXY, timeout=15)
    try:
        status, h, data = net.probe(url, {"Range": "bytes=0-1023"}, limit=1024)
    finally:
        net.close()
    ok = status in (200, 206) and _looks_like_media(h, data)
    return ok, _total_size(status, h), status == 206, status


def _is_faststart(path: str) -> bool:
    """True if the mp4's 'moov' index comes before the media data (starts streaming without a full download)."""
    try:
        with open(path, "rb") as f:
            pos, size = 0, os.path.getsize(path)
            for _ in range(64):
                f.seek(pos)
                head = f.read(16)
                if len(head) < 8:
                    return False
                n, kind = int.from_bytes(head[:4], "big"), head[4:8]
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if n == 1:
                    n = int.from_bytes(head[8:16], "big")
                if n < 8 or pos + n > size:
                    return False
                pos += n
    except OSError:
        pass
    return False


def _download_file(
    url: str, headers: dict, out_path: str, progress=None, check=None, workers: int | None = None,
    ctx=None, chunk: int | None = None,
) -> str:
    """Download a direct file. Parallel Range requests (adaptive concurrency, retries with backoff) when the
    server supports them, else one stream. Raises _SegmentError when the file cannot be fetched."""
    check = check or _noop
    workers = max(1, min(workers or config.DL_WORKERS, 8))
    chunk = chunk or config.DL_CHUNK_MB * 1024 * 1024
    net = _Net(headers, config.PROXY, ctx=ctx)
    gate = _Gate(workers)
    host = urlsplit(url).netloc
    part = out_path + ".part"
    t0 = time.monotonic()
    samples: list[tuple[float, int]] = [(t0, 0)]
    st = {"bytes": 0, "chunks": 0, "n": 0, "total": 0, "emit": 0.0, "err": None, "next": 0}
    lock = threading.Lock()

    def emit(force: bool = False, stage: str = "download"):
        if not progress:
            return
        now = time.monotonic()
        if not force and now - st["emit"] < 0.5:
            return
        st["emit"] = now
        samples.append((now, st["bytes"]))
        while len(samples) > 2 and now - samples[0][0] > 10:
            samples.pop(0)
        dt = max(now - samples[0][0], 0.001)
        speed = (st["bytes"] - samples[0][1]) / dt if dt >= 1 else st["bytes"] / max(now - t0, 0.001)
        total = st["total"]
        progress({
            "pct": 100 * st["bytes"] / total if total else 0, "speed": speed,
            "eta": (total - st["bytes"]) / speed if total and speed > 0 else 0, "done": st["bytes"],
            "seg_done": st["chunks"], "seg_total": st["n"], "workers": gate.limit, "host": host,
            "stage": stage, "unit": "chunks",
        })

    def sleep(seconds: float):
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            check()
            time.sleep(min(0.4, left))

    def with_retries(call, what: str, attempts: int = MAX_ATTEMPTS):
        attempt, free, last = 0, FREE_RETRIES, "error"
        while attempt < attempts:
            check()
            retry_after, cut = 0.0, False
            try:
                with gate:
                    res = call()
                gate.good()
                return res
            except _HTTPStatus as exc:
                last = f"HTTP {exc.code}"
                if exc.code in (400, 401, 403, 404, 410, 416):
                    raise _SegmentError(f"{host}: {what} failed - {last}") from None
                cut, retry_after = gate.bad(), exc.retry_after
            except (http.client.HTTPException, OSError) as exc:
                last = type(exc).__name__
                cut = gate.bad()
            if cut and free > 0:
                free -= 1
            else:
                attempt += 1
            if attempt < attempts:
                sleep(_backoff(attempt, retry_after))
        raise _SegmentError(f"{host}: {what} failed - {last}")

    def single_stream():
        def once():
            written = 0
            with open(part, "wb") as f:
                gen = net.stream(url)
                status, hdrs = next(gen)
                if status != 200:
                    raise _HTTPStatus(status)
                st["total"] = int(hdrs.get("content-length", 0) or 0)
                for data in gen:
                    check()
                    f.write(data)
                    written += len(data)
                    st["bytes"] = written
                    emit()
            return written
        with_retries(once, "download", attempts=4)

    def ranged(total: int):
        n = -(-total // chunk)
        st["n"], st["total"] = n, total
        with open(part, "wb") as f:
            f.truncate(total)
        out = open(part, "r+b")

        def fetch_range(i: int) -> bytes:
            a, b = i * chunk, min(total, (i + 1) * chunk) - 1

            def call():
                data = net.get(url, {"Range": f"bytes={a}-{b}"})
                if len(data) != b - a + 1:
                    raise OSError("short or unranged body")
                return data
            return with_retries(call, f"chunk {i + 1}/{n}")

        def worker():
            try:
                while True:
                    with lock:
                        if st["err"] or st["next"] >= n:
                            return
                        i = st["next"]
                        st["next"] += 1
                    data = fetch_range(i)
                    with lock:
                        out.seek(i * chunk)
                        out.write(data)
                        st["bytes"] += len(data)
                        st["chunks"] += 1
                    emit()
            except BaseException as exc:  # noqa: BLE001  (incl. JobCancelled)
                with lock:
                    st["err"] = st["err"] or exc

        try:
            threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(workers, n))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            out.close()
        if st["err"]:
            raise st["err"]

    try:
        def first():
            status, h, _data = net.probe(url, {"Range": "bytes=0-0"}, limit=1)
            if status not in (200, 206):  # 5xx/429 are retried, 403/404 fail at once
                raise _HTTPStatus(status, _retry_after(h.get("retry-after")))
            return status, h

        status, h = with_retries(first, "probe", attempts=5)
        total = _total_size(status, h)
        free = free_mb(os.path.dirname(out_path) or ".")
        if total and free < total / 1048576 * 1.1 + 50:  # the file itself plus a little room
            raise DiskSpaceError(
                f"Not enough free disk space: the video is {total / 1048576:.0f} MB (plus about 50 MB of working room), "
                f"only {free:.0f} MB are free. Free some space, or pick a lower quality (MAX_HEIGHT)."
            )
        if status == 206 and total > chunk:
            try:
                ranged(total)
            except _SegmentError as exc:
                if "short or unranged" not in str(exc):
                    raise
                log.warning("server ignores Range requests - downloading as one stream")
                st.update(bytes=0, chunks=0, n=0)
                single_stream()
        else:
            single_stream()
        size = os.path.getsize(part)
        if st["total"] and size != st["total"]:
            raise _SegmentError(f"{host}: incomplete download ({size} of {st['total']} bytes)")
        emit(force=True, stage="finalize")
        os.replace(part, out_path)
        return out_path
    finally:
        net.close()
        if os.path.exists(part):
            os.remove(part)


def playable_file(url: str, headers: dict) -> bool:
    try:
        return probe_file(url, headers)[0]
    except Exception:  # noqa: BLE001
        return False


async def _to_mp4(src: str, dst: str) -> str:
    """Make a streamable mp4: keep an mp4 whose index is already at the front, remux everything else (stream copy)."""
    if src.lower().endswith(".mp4") and (_is_faststart(src) or not tool("ffmpeg")):
        if not tool("ffmpeg"):
            log.warning("no ffmpeg: keeping the mp4 as downloaded (it will not start playing before it is fully loaded)")
        os.replace(src, dst)
        return dst
    rc, _, err = await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?",
                            "-c", "copy", "-movflags", "+faststart", dst)
    if rc != 0 or not os.path.exists(dst):
        raise RuntimeError("ffmpeg could not convert the file to mp4: " + _short_err(err))
    os.remove(src)
    return dst


async def download_file(url: str, headers: dict, workdir: str, name: str, progress=None, check=None) -> str:
    os.makedirs(workdir, exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", name).strip("_")[:60] or "video"
    ext = os.path.splitext(urlsplit(url).path)[1].lower() or ".mp4"
    raw, mp4 = os.path.join(workdir, f"{safe}.src{ext}"), os.path.join(workdir, f"{safe}.mp4")
    try:
        await asyncio.to_thread(_download_file, url, headers, raw, progress, check)
    except DiskSpaceError:
        raise
    except _SegmentError as exc:
        raise SegmentFailure(str(exc)) from exc
    return await _to_mp4(raw, mp4)


class SegmentFailure(RuntimeError):
    """The stream's segments could not be fetched from any mirror (host down, blocked or throttling)."""

    def __init__(self, msg: str = "", blocked: bool = False):
        super().__init__(msg)
        self.blocked = blocked  # HTTP 403 / 429 at the end: waiting, a fresh link and trying again may work


class DownloadBlocked(RuntimeError):
    """Every mirror refused with HTTP 403 / 429. The caller may wait, fetch a fresh stream link and call again
    with the same `resume` dict to continue where the download stopped."""


async def download_m3u8(
    url: str, headers: dict, workdir: str, name: str, progress=None, mirrors=(), check=None, resume: dict | None = None
) -> str:
    """Download an HLS stream to an .mp4 in `workdir`. `progress` is a sync callable (called from a
    worker thread) receiving {"pct","speed","eta","done", ...}. `mirrors` are other hosts serving the same
    stream (same path): segments fail over to them. `check()` may raise to cancel (called often).
    Tries the built-in downloader first, then yt-dlp, then plain ffmpeg - except when the built-in one
    proved the segments are unreachable, because the others would hit the same wall (and spend minutes
    doing it). If everything fails the error lists what each one said."""
    os.makedirs(workdir, exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", name).strip("_")[:60] or "video"
    outtmpl = os.path.join(workdir, f"{safe}.%(ext)s")
    mp4 = os.path.join(workdir, f"{safe}.mp4")
    errors: list[str] = []

    async def native():
        return await asyncio.to_thread(_native_download, url, headers, mp4, progress, mirrors, check, None, None, resume)

    async def ytdlp():
        return await asyncio.to_thread(_ytdlp_download, url, headers, outtmpl, progress, check)

    async def ffmpeg():
        return await _ffmpeg_download(url, headers, mp4)

    path = None
    for label, fn in (("native", native), ("yt-dlp", ytdlp), ("ffmpeg", ffmpeg)):
        try:
            path = await fn()
            if path and not os.path.exists(path) and os.path.exists(os.path.splitext(path)[0] + ".mp4"):
                path = os.path.splitext(path)[0] + ".mp4"
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                break
            errors.append(f"{label}: output file is missing")
            path = None
        except _SegmentError as exc:
            log.warning("native download gave up: %s", exc)
            raise SegmentFailure(str(exc), exc.blocked) from exc
        except ToolMissing:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = _short_err(str(exc), 2) or type(exc).__name__
            errors.append(f"{label}: {msg}")
            log.warning("%s download failed: %s", label, msg)
            path = None
    if not path:
        raise RuntimeError("\n".join(errors))
    return path


async def download_any(urls: list[str], headers: dict, workdir: str, name: str, progress=None, check=None,
                       resume: dict | None = None):
    """Download from the first URL that works, falling back to the next one when a download fails.
    URLs may be HLS playlists or direct video files (other qualities / servers of the same video).
    HLS URLs with the same path are mirrors of one stream: they are used for per-segment failover inside a
    download, so after the segments proved unreachable they are not tried again one by one.
    Returns (url_used, path). Raises RuntimeError listing what failed."""
    errors: list[str] = []
    exhausted: set[str] = set()  # paths whose mirrors all failed on segments
    n_blocked = n_other = 0
    for i, u in enumerate(urls):
        host, path_ = urlparse(u).netloc, urlparse(u).path
        direct = is_file_url(u)
        if path_ in exhausted and not direct:
            continue
        if i and not await asyncio.to_thread(playable_file if direct else playable, u, headers):
            errors.append(f"{host}: not reachable")
            continue
        try:
            if direct:
                return u, await download_file(u, headers, workdir, name, progress, check)
            mirrors = [x for x in urls if x != u and not is_file_url(x) and urlparse(x).path == path_]
            return u, await download_m3u8(u, headers, workdir, name, progress, mirrors=mirrors, check=check,
                                          resume=None if direct else resume)
        except (ToolMissing, DiskSpaceError):
            raise
        except SegmentFailure as exc:
            if not direct:  # same-path HLS URLs are mirrors that were already tried per segment
                exhausted.add(path_)
            if exc.blocked:
                n_blocked += 1
            else:
                n_other += 1
            errors.append(str(exc)[:160])
        except RuntimeError as exc:
            n_other += 1
            log.warning("%s failed: %s", host, exc)
            errors.append(f"{host}: {str(exc)[:160]}")
    msg = "Download failed on every mirror.\n" + "\n".join(errors[-4:])
    if n_blocked and not n_other:
        raise DownloadBlocked(msg)
    raise RuntimeError(msg)


def _describe(url: str, headers: dict) -> list[str]:
    net = _Net(headers, config.PROXY, timeout=15)
    try:
        if is_file_url(url):
            ok, total, ranges, status = probe_file(url, headers)
            size = f"{total / 1048576:.1f} MB" if total else "unknown size"
            return [f"📼 Direct video file, {size}; " + ("fast parallel downloads (Range supported)" if ranges else "no Range support: one stream")]
        text = net.get(url).decode("utf-8", "ignore")
        lines, media_text = [], text
        if "#EXT-X-STREAM-INF" in text:
            vs = _rank_variants_ex(text, url, config.MAX_HEIGHT)
            heights = ", ".join(f"{v['height']}p" if v["height"] else "?" for v in vs)
            lines.append(f"🎞 HLS master playlist: {len(vs)} variant(s) ({heights}); the bot takes the best up to {config.MAX_HEIGHT or 'any'}p")
            groups = _audio_renditions(text, url)
            pa = pick_audio(text, url, vs[0]["audio"]) if vs and vs[0]["audio"] else None
            if pa:
                lines.append(f"🔊 Audio: separate rendition '{pa['name'] or pa['uri'].rsplit('/', 1)[-1]}' - downloaded and muxed in")
            elif groups:
                lines.append("🔊 Audio renditions exist but the chosen variant does not use one")
            else:
                lines.append("🔊 Audio: inside the video segments")
            media_text = net.get(vs[0]["url"]).decode("utf-8", "ignore") if vs else ""
        try:
            segs, init = _parse_media(media_text, url)
            dur = sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", media_text))
            enc = "AES-128" if "METHOD=AES-128" in media_text else "none"
            lines.append(f"📦 {len(segs)} segment(s), ~{int(dur // 60)}m {int(dur % 60):02d}s, {'fMP4' if init else 'MPEG-TS'}, encryption: {enc}")
        except _HLSError as exc:
            lines.append(f"⚠️ {exc}")
        return lines
    except Exception as exc:  # noqa: BLE001
        return [f"⚠️ Could not describe the stream ({type(exc).__name__})"]
    finally:
        net.close()


async def describe(url: str, headers: dict) -> list[str]:
    """What kind of stream this is (for /probe): HLS variants, separate audio, segments / direct file."""
    return await asyncio.to_thread(_describe, url, headers)
