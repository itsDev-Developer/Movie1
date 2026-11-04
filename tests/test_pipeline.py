"""Offline end-to-end test of download -> split -> upload -> progress / cancel / errors / debug trail.

    python tests/test_pipeline.py        (needs ffmpeg + an HLS test stream in /tmp/hls, see make_stream())

Telegram is a fake client; only the embed-page extraction is replaced (it needs httpx and a real site).
"""
import asyncio
import os as _os
_os.environ['CAPTURE_MODE'] = 'off'  # these tests are about the static path; test_capture.py covers the browser
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.update(BOT_TOKEN="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", API_HASH="HASHHASHHASHHASH1234", OWNER_ID="100",
                  DL_MIN_INTERVAL_MS="10", DL_ROUNDS="2", DL_BLOCK_WAIT="5", MAX_PARALLEL="1")
os.chdir(tempfile.mkdtemp())  # logs/ and downloads/ go here

# ---- stubs for what is not installed here
class _Any:
    def __init__(self, *a, **k): self.a, self.k = a, k
    def __getattr__(self, n): return _Any()

for name in ("pyrogram", "pyrogram.types", "pyrogram.errors", "httpx", "yt_dlp"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["pyrogram.types"].InlineKeyboardButton = _Any
sys.modules["pyrogram.types"].InlineKeyboardMarkup = _Any
class RPCError(Exception): pass
sys.modules["pyrogram.errors"].RPCError = RPCError

import config  # noqa: E402
import debuglog  # noqa: E402
import downloader  # noqa: E402
import m3u8  # noqa: E402

STREAM = os.environ.get("HLS_DIR", "/tmp/hls")


def make_stream():
    if os.path.exists(os.path.join(STREAM, "index.m3u8")):
        return
    os.makedirs(STREAM, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "24", "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
                    "-c:a", "aac", "-b:a", "48k", "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
                    "-hls_segment_filename", os.path.join(STREAM, "seg%03d.ts"), os.path.join(STREAM, "index.m3u8")], check=True)


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    slow = False
    def log_message(self, *a): pass
    def do_GET(self):  # noqa: N802
        path = os.path.join(STREAM, self.path.lstrip("/").split("?")[0])
        if not os.path.isfile(path):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        if H.slow and path.endswith(".ts"):
            import time; time.sleep(0.6)
        body = open(path, "rb").read()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


class FakeMsg:
    def __init__(self, store, i=1): self.store, self.id = store, i
    async def edit_text(self, text, **k): self.store.append(text)


class FakeClient:
    def __init__(self):
        self.cards, self.sent, self.dms, self.video_fails, self.stopped = [], [], [], False, False
    async def send_message(self, chat, text, **k):
        if chat == config.OWNER_ID and "Download failed" in text:
            self.dms.append(text)
            return FakeMsg([])
        self.cards.append(text)
        return FakeMsg(self.cards)
    def stop_transmission(self): self.stopped = True; raise StopAsyncIteration
    async def _send(self, kind, chat, path, progress=None, **k):
        if kind == "video" and self.video_fails:
            raise RPCError("VIDEO_CONTENT_TYPE_INVALID")
        size = os.path.getsize(path)
        def run():
            for frac in (0.25, 0.5, 1.0):
                progress(int(size * frac), size)
        await asyncio.to_thread(run)
        self.sent.append((kind, os.path.basename(path), size, k.get("duration"), k.get("supports_streaming")))
        return FakeMsg([], len(self.sent))
    async def send_video(self, chat, path, **k): return await self._send("video", chat, path, **k)
    async def send_document(self, chat, path, **k): return await self._send("document", chat, path, **k)


class FakeQ:
    def __init__(self, uid=555, name="Sam"):
        self.from_user = types.SimpleNamespace(id=uid, first_name=name)
        self.message = types.SimpleNamespace(chat=types.SimpleNamespace(id=uid))
        self.answers = []
    async def answer(self, text=None, show_alert=False): self.answers.append(text)


fails = 0
def check(label, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    fails += 0 if cond else 1


async def run_job(client, q, title="Test Movie (2024)", wait=60):
    await downloader.start(client, q, title, "https://embed.example/movie/1")
    job = downloader.BY_USER.get(q.from_user.id)
    if job and job.task:
        await asyncio.wait_for(asyncio.shield(job.task), wait) if False else None
    for _ in range(int(wait * 10)):
        if q.from_user.id not in downloader.BY_USER:
            break
        await asyncio.sleep(0.1)
    return job


async def main():
    make_stream()
    downloader.Card.INTERVAL = 0  # show every update (the real bot edits at most every 3 s)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    mode = {"found": True}

    async def fake_extract(url, referer=None, trace=None, alternates=None, **k):
        if mode["found"]:
            return f"{base}/index.m3u8", {"User-Agent": m3u8.UA, "Referer": "https://embed.example/"}
        (trace if trace is not None else []).append("https://embed.example/movie/1?token=SECRETTOK -> HTTP 404")
        return None, {}
    m3u8.extract_m3u8 = fake_extract

    # ---------- 1. normal: split under a tiny limit so it becomes parts, uploaded as streamable videos
    config.MAX_UPLOAD_BYTES = 300 * 1024
    cl, q = FakeClient(), FakeQ()
    await run_job(cl, q)
    check("download + split + upload finishes", cl.cards and "Sent" in cl.cards[-1], cl.cards[-1].replace("\n", " ")[:80])
    n = len(cl.sent)
    check("movie larger than the limit is uploaded in several parts", n >= 2, f"{n} parts: " + ", ".join(f"{s[2] // 1024} KB" for s in cl.sent))
    check("every part is under the upload limit", all(s[2] <= config.MAX_UPLOAD_BYTES for s in cl.sent))
    check("parts are sent as streamable videos with a duration", all(s[0] == "video" and s[3] and s[4] for s in cl.sent))
    check("progress card showed download and upload bars with speed", any("Download" in c and "▰" in c and "/s" in c for c in cl.cards) and any("Upload" in c and "▰" in c and "part" in c for c in cl.cards))
    check("final message shows the real total size", "Sent • 0 B" not in cl.cards[-1] and "3 parts" in cl.cards[-1], cl.cards[-1].split("\n")[-1])
    check("job folder and per-user lock are cleaned up", not downloader.BY_USER and not os.listdir(config.DOWNLOAD_DIR))
    t = debuglog.RECENT[0]
    check("trail records the whole run (stream, download, parts, uploads)", t["state"] == "done" and all(
        any(w in l for l in t["lines"]) for w in ("stream:", "downloaded", "parts:", "uploaded part 1/", "end: done")))

    # ---------- 2. whole file fits: one upload
    config.MAX_UPLOAD_BYTES = 1950 * 1024 * 1024
    cl, q = FakeClient(), FakeQ()
    await run_job(cl, q)
    check("a file under 2 GB is sent as one video", len(cl.sent) == 1 and "Sent" in cl.cards[-1])

    # ---------- 3. Telegram refuses it as a video -> falls back to a document
    cl, q = FakeClient(), FakeQ()
    cl.video_fails = True
    await run_job(cl, q)
    check("send_video refused -> same file goes out as a document", cl.sent and cl.sent[0][0] == "document" and "Sent" in cl.cards[-1])
    check("   and the trail says so", any("retrying" in l for l in debuglog.RECENT[0]["lines"]))

    # ---------- 4. no stream on the page: the user sees no urls, the owner is told
    mode["found"] = False
    cl, q = FakeClient(), FakeQ()
    await run_job(cl, q)
    shown = cl.cards[-1]
    ref = debuglog.RECENT[0]["ref"]
    check("failure: user sees a short reason + reference, no link / token", "Reference" in shown and ref in shown and "http" not in shown and "SECRETTOK" not in shown, shown.replace("\n", " ")[:90])
    check("failure: the owner gets a message with the reference and the reason", cl.dms and ref in cl.dms[0] and "No stream found" in cl.dms[0] and "SECRETTOK" not in cl.dms[0])
    d = debuglog.find(ref)
    check("/debug REF has the full detail (and no secret token)", d and "HTTP 404" in debuglog.render(d) and "SECRETTOK" not in debuglog.render(d))
    check("/debug list shows the failed job", ref in debuglog.summary())
    mode["found"] = True

    # ---------- 5. the owner sees the reason in the card; nobody else
    cl, q = FakeClient(), FakeQ(uid=config.OWNER_ID, name="Owner")
    mode["found"] = False
    await run_job(cl, q)
    check("owner's own failure shows the detail in the card, no DM to self", "No stream found" in cl.cards[-1] and not cl.dms)
    mode["found"] = True

    # ---------- 6. cancel while downloading
    H.slow = True
    cl, q = FakeClient(), FakeQ()
    await downloader.start(cl, q, "Slow Movie", "https://embed.example/movie/2")
    await asyncio.sleep(2.0)
    cq = FakeQ()
    await downloader.cancel(cl, cq, q.from_user.id)
    for _ in range(100):
        if q.from_user.id not in downloader.BY_USER:
            break
        await asyncio.sleep(0.1)
    H.slow = False
    check("cancel stops the download and says so", "Cancelled" in cl.cards[-1] and debuglog.RECENT[0]["state"] == "cancelled")
    check("   and cleans up", not downloader.BY_USER and not os.listdir(config.DOWNLOAD_DIR))

    # ---------- 7. one download per user, and the allow-list
    cl, q = FakeClient(), FakeQ()
    H.slow = True
    await downloader.start(cl, q, "A", "https://embed.example/movie/3")
    q2 = FakeQ(uid=q.from_user.id)
    await downloader.start(cl, q2, "B", "https://embed.example/movie/4")
    check("a second download from the same user is refused", any("already" in (a or "") for a in q2.answers))
    downloader.BY_USER[q.from_user.id].stop()
    await asyncio.sleep(1)
    H.slow = False
    config.DOWNLOAD_USERS = {777}
    q3 = FakeQ(uid=888)
    await downloader.start(cl, q3, "C", "https://embed.example/movie/5")
    check("with DOWNLOAD_USERS set, others are refused", any("not available" in (a or "") for a in q3.answers) and 888 not in downloader.BY_USER)
    config.DOWNLOAD_USERS = set()

    # ---------- 8. scrubbing of anything stored
    check("bot token / hash never reach the trail", "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in debuglog.scrub("x " + os.environ["BOT_TOKEN"]))
    srv.shutdown()
    print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
