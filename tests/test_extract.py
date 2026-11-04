"""Offline tests of the embed-page extractor against fake embed sites + the extraction dump.

    python tests/test_extract.py

httpx is replaced by a tiny urllib shim when it is not installed, so the extractor code itself is what runs.
"""
import asyncio
import os as _os
_os.environ['CAPTURE_MODE'] = 'off'  # these tests are about the static path; test_capture.py covers the browser
import http.server
import io
import os
import sys
import threading
import types
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.update(BOT_TOKEN="1:" + "a" * 35, API_HASH="h" * 20, OWNER_ID="1", BASE_URL="http://embed.test")
STREAM = os.environ.get("HLS_DIR", "/tmp/hls")

try:
    import httpx  # noqa: F401
except ImportError:
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
                req = urllib.request.Request(url, headers=headers or {})
                try:
                    with urllib.request.urlopen(req, timeout=15) as r:
                        return _R(r.status, r.read(), r.geturl(), dict(r.headers))
                except urllib.error.HTTPError as e:
                    return _R(e.code, e.read(), url, dict(e.headers))
            return await asyncio.to_thread(run)

    shim.AsyncClient = AsyncClient
    sys.modules["httpx"] = shim

import config  # noqa: E402
import m3u8  # noqa: E402

PORT = {}


def site(path: str) -> tuple[int, str, str]:
    """-> (status, content-type, body) of the fake embed provider."""
    P = f"http://127.0.0.1:{PORT['n']}"
    if path == "/embed/movie/chain":          # iframe -> {src:} -> .attr(src) -> playlist without .m3u8
        return 200, "text/html", f'<html><body><iframe id="player_iframe" src="//127.0.0.1:{PORT["n"]}/rcp/abc" allowfullscreen></iframe></body></html>'
    if path == "/rcp/abc":
        return 200, "text/html", '<script>function loadIframe(){ var o = {id:"player", src: "/prorcp/def", class:"x"}; $("<iframe>", o).appendTo("body"); }</script>'
    if path == "/prorcp/def":
        return 200, "text/html", f'<script src="/js/player.js"></script><div id="player"></div>'
    if path == "/js/player.js":
        return 200, "application/javascript", f'new Playerjs({{id:"player", file:"{P}/hls/xyz/master", poster:"/p.jpg"}});'
    if path == "/embed/movie/attr":
        return 200, "text/html", "<script>$('#frame').attr('src', '/embed/inner/1');</script>"
    if path == "/embed/inner/1":
        return 200, "text/html", f'<script>var hls = new Hls(); hls.loadSource("{P}/hls/q/index.m3u8");</script>'
    if path == "/embed/movie/meta":
        return 200, "text/html", '<meta http-equiv="refresh" content="0; url=/embed/movie/loc">'
    if path == "/embed/movie/loc":
        return 200, "text/html", "<script>window.location.href = '/embed/inner/1';</script>"
    if path == "/embed/movie/cf":
        return 403, "text/html", "<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/x'></script></body></html>"
    if path == "/embed/movie/empty":
        return 200, "text/html", "<html><body>Movie not found</body></html>"
    return 404, "text/plain", "nope"


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/hls/"):                      # the stream itself (any name -> the test playlist)
            name = path.rsplit("/", 1)[1]
            f = os.path.join(STREAM, "index.m3u8" if name in ("master", "index.m3u8") else name)
            if os.path.isfile(f):
                body = open(f, "rb").read()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        status, ctype, body = site(path)
        data = body.encode()
        self.send_response(status); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


fails = 0
def check(label, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    fails += 0 if cond else 1


async def extract(path, **k):
    trace, dump = [], m3u8.Dump()
    url, hdr = await m3u8.extract_m3u8(f"http://127.0.0.1:{PORT['n']}{path}", referer="http://embed.test/", trace=trace, dump=dump, **k)
    return url, trace, dump


async def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    PORT["n"] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url, trace, dump = await extract("/embed/movie/chain")
    check("iframe -> {src:'/prorcp/..'} -> external script -> playlist URL without .m3u8", bool(url) and url.endswith("/hls/xyz/master"), str(url))
    z = zipfile.ZipFile(io.BytesIO(dump.zip_bytes()))
    names = z.namelist()
    rep = z.read("report.txt").decode()
    check("dump zip has report.txt and one file per page", "report.txt" in names and sum(n.startswith("pages/") for n in names) >= 4, f"{len(names)} files")
    check("report lists every hop, the candidate and the result", all(w in rep for w in ("/rcp/abc", "/prorcp/def", "player.js", "PLAYLIST OK", "RESULT:")))
    check("page files hold the raw text", any(b"Playerjs" in z.read(n) for n in names if n.startswith("pages/")))

    url, trace, dump = await extract("/embed/movie/attr")
    check(".attr('src', ...) hop then hls.loadSource(...)", bool(url) and url.endswith("/hls/q/index.m3u8"), str(url))

    url, trace, dump = await extract("/embed/movie/meta")
    check("<meta refresh> then location.href hop", bool(url) and url.endswith("index.m3u8"), str(url))

    url, trace, dump = await extract("/embed/movie/cf")
    check("a Cloudflare challenge page gives no stream but a clear reason", url is None and any("bot-protection" in t for t in trace), trace[0][:110] if trace else "")
    check("   with the advice in the report", "curl_cffi" in dump.report())

    url, trace, dump = await extract("/embed/movie/empty")
    check("a page without a player: no stream, and the dump says what the page was", url is None and any(b"Movie not found" in zipfile.ZipFile(io.BytesIO(dump.zip_bytes())).read(n) for n in zipfile.ZipFile(io.BytesIO(dump.zip_bytes())).namelist() if n.startswith("pages/")) and "no stream found" in dump.report())

    # template hosts: https://tm.{v1}/... with the domain named elsewhere in the page
    cands, tpl = m3u8.expand_templates(['x={"v1":"cdn.shadowhost.com"}; file:"https://tm.{v1}/pl/9/master.m3u8 or https://tm.{v2}/pl/9/master.m3u8"'], "https://embed.test/p")
    check("{v1} templates are filled with the domains found in the page", any(c == "https://tm.cdn.shadowhost.com/pl/9/master.m3u8" for c in cands), f"{len(cands)} candidates, templates={len(tpl)}")
    check("a blocked page's challenge text is recognised, a real player page is not", m3u8.challenge_hint("<title>Just a moment...</title>") and not m3u8.challenge_hint("<video></video>" + "x" * 4000 + "new Hls(); .m3u8 Just a moment"))

    srv.shutdown()
    print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
