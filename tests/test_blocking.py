"""Offline tests of the HLS downloader against a local server that behaves like a blocking CDN.

    python tests/test_blocking.py            (needs ffmpeg; no Telegram, no MongoDB, no internet)

Scenarios: 403 unless a header is present, 429 when requests come too fast, a host that blocks half way (resume),
a host whose segments only work with a cookie it hands out on the playlist, and a host that never opens up.
"""
import http.server
import os as _os
_os.environ['CAPTURE_MODE'] = 'off'
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
for k, v in dict(API_ID="1", API_HASH="h" * 32, BOT_TOKEN="1:" + "a" * 35, OWNER_ID="1", MONGO_URI="mongodb://x",
                 DL_MIN_INTERVAL_MS="20", BLOCK_ATTEMPTS="10").items():
    os.environ.setdefault(k, v)
try:
    import httpx  # noqa: F401
except ImportError:  # the downloader only needs httpx for page extraction, not for these tests
    import types
    sys.modules["httpx"] = types.ModuleType("httpx")

import m3u8  # noqa: E402

STREAM = os.environ.get("HLS_DIR", "/tmp/hls")
S = {}  # scenario state, reset by each test


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        S["hits"] = S.get("hits", 0) + 1
        name = self.path.split("?")[0].lstrip("/")
        path = os.path.join(STREAM, name)
        if not os.path.isfile(path):
            return self._send(404)
        is_seg = name.endswith(".ts")
        S.setdefault("seen", []).append(dict(self.headers))
        mode = S.get("mode")
        if mode == "needs_origin" and "Origin" not in self.headers:
            return self._send(403, b"forbidden")
        if mode == "needs_cookie":
            if not is_seg:
                return self._send(200, open(path, "rb").read(), {"Set-Cookie": "__cf_bm=abc123; Path=/; HttpOnly"})
            if "__cf_bm=abc123" not in self.headers.get("Cookie", ""):
                return self._send(403)
        if mode == "rate_limit" and is_seg:
            now = time.monotonic()
            win = [t for t in S.setdefault("win", []) if now - t < 1.0]
            win.append(now)
            S["win"] = win
            if len(win) > 6:  # more than 6 requests in one second
                S["n429"] = S.get("n429", 0) + 1
                return self._send(429, b"slow down", {"Retry-After": "1"})
        if mode == "block_after" and is_seg:
            S["segs"] = S.get("segs", 0) + 1
            if S["segs"] > S["allow"] and time.monotonic() < S["open_at"]:
                return self._send(403)
        if mode == "never" and is_seg:
            return self._send(403)
        self._send(200, open(path, "rb").read())


def serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def probe_ok(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def run(name, mode, base, headers=None, resume=None, **state):
    S.clear()
    S.update(mode=mode, **state)
    work = tempfile.mkdtemp()
    out = os.path.join(work, "v.mp4")
    t0 = time.time()
    headers = headers or {"User-Agent": m3u8.UA, "Referer": "https://player.example/"}
    try:
        m3u8._native_download(f"{base}/index.m3u8", headers, out, resume=resume, workers=4)
        dur = probe_ok(out)
        return True, dur, time.time() - t0, work
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc} (blocked={getattr(exc, 'blocked', None)})", time.time() - t0, work


def main():
    srv, base = serve()
    fails = 0

    def check(label, cond, extra=""):
        nonlocal fails
        print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
        fails += 0 if cond else 1

    ok, dur, secs, w = run("plain", None, base)
    check("baseline: unblocked host downloads the whole stream", ok and abs(dur - 24) < 1.5, f"{dur} s of video in {secs:.1f}s")
    shutil.rmtree(w, ignore_errors=True)

    ok, dur, secs, w = run("origin", "needs_origin", base)
    check("403 without Origin header -> rotates to a header profile that works", ok and abs(dur - 24) < 1.5, f"{secs:.1f}s")
    shutil.rmtree(w, ignore_errors=True)

    ok, dur, secs, w = run("cookie", "needs_cookie", base)
    check("403 without the CDN cookie handed out on the playlist -> cookie jar fixes it", ok and abs(dur - 24) < 1.5)
    shutil.rmtree(w, ignore_errors=True)

    ok, dur, secs, w = run("rate", "rate_limit", base)
    check("429 when requests come too fast -> pauses, slows down, still finishes", ok and abs(dur - 24) < 1.5 and secs < 15,
          f"{S.get('n429', 0)} x 429, {secs:.1f}s")
    shutil.rmtree(w, ignore_errors=True)

    # the host blocks after 10 segments for 4 s, longer than the downloader's own patience (BLOCK_ATTEMPTS=3)
    os.environ["BLOCK_ATTEMPTS"] = "10"
    m3u8.config.BLOCK_ATTEMPTS = 4
    resume = {}
    ok, info, secs, w = run("resume1", "block_after", base, resume=resume, allow=10, open_at=time.monotonic() + 1e9)
    check("a host that blocks half way -> first attempt reports 'blocked'", (not ok) and "blocked=True" in str(info), str(info)[:110])
    size1, done1 = resume.get("v_size", 0), resume.get("v", 0)
    check("   the partial download is kept for a resume", done1 >= 8 and size1 > 0, f"{done1} segments on disk")
    # second attempt (the block is over): continues, does not start again
    S["open_at"] = 0
    segs_before = S["segs"]
    out = os.path.join(w, "v.mp4")
    try:
        m3u8._native_download(f"{base}/index.m3u8", {"User-Agent": m3u8.UA, "Referer": "https://player.example/"}, out, resume=resume, workers=4)
        ok2, dur2 = True, probe_ok(out)
    except Exception as exc:  # noqa: BLE001
        ok2, dur2 = False, str(exc)
    fetched_again = S["segs"] - segs_before
    check("   resume finishes the video", ok2 and abs(dur2 - 24) < 1.5, str(dur2))
    check("   resume fetched only the missing segments", fetched_again <= 24 - done1 + 2, f"{fetched_again} requests for {24 - done1} missing")
    shutil.rmtree(w, ignore_errors=True)

    m3u8.config.BLOCK_ATTEMPTS = 3
    t0 = time.time()
    ok, info, secs, w = run("never", "never", base)
    check("a host that never opens up -> gives up with blocked=True in bounded time", (not ok) and "blocked=True" in str(info) and secs < 90,
          f"{secs:.0f}s")
    shutil.rmtree(w, ignore_errors=True)

    # two proxies: the first always answers 403 (a blocked IP), the second is fine -> the downloader switches
    px_hits = {"a": 0, "b": 0}

    def make_proxy(tag, block):
        class P(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                px_hits[tag] += 1
                if block:
                    body = b"blocked"
                    self.send_response(403)
                else:
                    import urllib.request
                    req = urllib.request.Request(self.path, headers={k: v for k, v in self.headers.items() if k.lower() != "proxy-authorization"})
                    with urllib.request.urlopen(req) as r:
                        body = r.read()
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        px = http.server.ThreadingHTTPServer(("127.0.0.1", 0), P)
        threading.Thread(target=px.serve_forever, daemon=True).start()
        return px, f"http://127.0.0.1:{px.server_address[1]}"

    pa, ua = make_proxy("a", True)
    pb, ub = make_proxy("b", False)
    m3u8.config.PROXIES = [ua, ub]
    m3u8.config.BLOCK_ATTEMPTS = 14
    ok, dur, secs, w = run("proxy", None, base)
    check("blocked IP (first proxy 403s everything) -> rotates to the second proxy", ok and abs(dur - 24) < 1.5 and px_hits["b"] > 20,
          f"proxy a: {px_hits['a']} requests, proxy b: {px_hits['b']}, {secs:.1f}s")
    shutil.rmtree(w, ignore_errors=True)
    m3u8.config.PROXIES = []
    pa.shutdown()
    pb.shutdown()

    srv.shutdown()
    print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
