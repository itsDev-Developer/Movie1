"""Embed link -> download -> (split under 2 GB) -> upload to the chat. Same engine as the reference bot (m3u8.py).

Users see a progress card and, on failure, a short sentence + a reference code. The real reason (stream hosts, status
codes, tool output, tracebacks) goes into the job's trail (debuglog.py) and to the owner, who reads it with /debug."""
import asyncio
import html
import logging
import os
import random
import shutil
import threading
import time
import uuid

from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup

import config
import debuglog
import m3u8

log = logging.getLogger("downloader")


class JobCancelled(BaseException):
    """Raised inside worker threads when the user cancels. BaseException on purpose: the downloader's
    `except Exception` fallbacks must not swallow it."""


class JobError(RuntimeError):
    """`public` may be shown to anybody; `detail` goes to the owner only."""

    def __init__(self, public: str, detail: str = ""):
        super().__init__(public)
        self.public, self.detail = public, detail or public


class Job:
    def __init__(self, uid: int, chat_id: int, title: str, trail: debuglog.Trail, preset: dict | None = None):
        self.uid, self.chat_id, self.title, self.trail = uid, chat_id, title, trail
        self.preset = preset  # a stream already found (by /capture): skip the page extraction
        self.cancel = threading.Event()
        self.dump: m3u8.Dump | None = None  # what the extractor saw on the embed page
        self.task: asyncio.Task | None = None
        self.state = "queued"

    def check(self):
        if self.cancel.is_set():
            raise JobCancelled()

    def stop(self):
        self.cancel.set()
        if self.task and not self.task.done():
            self.task.cancel()


PRESETS: dict[str, dict] = {}  # streams found by /capture, for its Download buttons (kept for an hour)
BY_USER: dict[int, Job] = {}   # one running / waiting download per user
WAITING: list[Job] = []
_sem: asyncio.Semaphore | None = None


def is_owner(uid: int) -> bool:
    return bool(config.OWNER_ID) and uid == config.OWNER_ID


def allowed(uid: int) -> bool:
    return config.DOWNLOADS and (not config.DOWNLOAD_USERS or uid in config.DOWNLOAD_USERS or is_owner(uid))


# ---------------------------------------------------------------- formatting
def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def fmt_eta(seconds: float) -> str:
    s = int(max(seconds, 0))
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s" if s < 3600 else f"{s // 3600}h {s % 3600 // 60:02d}m"


def bar(pct: float, width: int = 12) -> str:
    n = max(0, min(width, round(width * pct / 100)))
    return "▰" * n + "▱" * (width - n)


PHASES = [("find", "Find the stream"), ("download", "Download"), ("prepare", "Prepare the file"), ("upload", "Upload to Telegram")]


class Card:
    """The progress message. Updates may come from any thread; edits are throttled."""

    INTERVAL = 3.0  # seconds between edits of the card (Telegram rate-limits message edits)

    def __init__(self, message, title: str, loop, markup=None):
        self.msg, self.title, self.loop, self.markup = message, title, loop, markup
        self.done: dict[str, str] = {}
        self.active = ""
        self.detail = ""
        self.wait = ""
        self.final = ""
        self._last, self._text = 0.0, ""
        self._lock = asyncio.Lock()

    def render(self) -> str:
        if self.final:
            return f"🎬 <b>{html.escape(self.title)}</b>\n\n{self.final}"
        lines = [f"🎬 <b>{html.escape(self.title)}</b>", ""]
        if self.wait:
            lines += [self.wait, ""]
        for key, label in PHASES:
            if key in self.done:
                lines.append(f"✅ {label}" + (f" • {html.escape(self.done[key])}" if self.done[key] else ""))
            elif key == self.active:
                lines.append(f"▶️ <b>{label}</b>" + (f"\n    {self.detail}" if self.detail else ""))
            else:
                lines.append(f"○ {label}")
        return "\n".join(lines)

    async def push(self, force: bool = False):
        text = self.render()
        now = time.monotonic()
        if text == self._text or (not force and now - self._last < self.INTERVAL):
            return
        async with self._lock:
            self._last, self._text = now, text
            try:
                await self.msg.edit_text(text, reply_markup=None if self.final else self.markup, disable_web_page_preview=True)
            except Exception:  # noqa: BLE001  (not modified / deleted / flood: the card is only cosmetic)
                pass

    def push_threadsafe(self):
        """Called from worker threads (download / upload progress)."""
        if time.monotonic() - self._last >= self.INTERVAL:
            asyncio.run_coroutine_threadsafe(self.push(), self.loop)

    async def begin(self, key: str, detail: str = ""):
        self.active, self.detail, self.wait = key, detail, ""
        await self.push(force=True)

    async def finish(self, key: str, note: str = ""):
        self.done[key] = note
        if self.active == key:
            self.active, self.detail = "", ""
        await self.push(force=True)


# ---------------------------------------------------------------- the job
def remember_stream(title: str, page: str, url: str, headers: dict, alternates: list[str]) -> str:
    """Keep a found stream for a button; returns the token the button carries."""
    now = time.time()
    for k in [k for k, v in PRESETS.items() if now - v["t"] > 3600]:
        PRESETS.pop(k, None)
    token = uuid.uuid4().hex[:8]
    PRESETS[token] = {"title": title, "page": page, "url": url, "headers": headers, "alternates": alternates, "t": now}
    return token


async def start_preset(client, q, token: str) -> None:
    p = PRESETS.get(token)
    if not is_owner(q.from_user.id):
        await q.answer("You can't use this.", show_alert=True)
    elif not p:
        await q.answer("This button expired. Run /capture again.", show_alert=True)
    else:
        await start(client, q, p["title"], p["page"], preset=p)


async def start(client, q, title: str, url: str, referer: str = "", preset: dict | None = None) -> None:
    """The Download button was pressed (q = the callback query)."""
    uid = q.from_user.id
    if not allowed(uid):
        await q.answer("Downloads are not available for your account.", show_alert=True)
        return
    if uid in BY_USER:
        await q.answer("You already have a download in progress.", show_alert=True)
        return
    await q.answer("Starting…")
    trail = debuglog.begin(title, q.from_user)
    trail.add(f"requested by {trail.who}: {title}")
    trail.add(f"embed page: {url}")
    job = Job(uid, q.message.chat.id, title, trail, preset)
    BY_USER[uid] = job
    cancel_kb = Markup([[Btn("✖ Cancel", callback_data=f"cx:{uid}")]])
    msg = await client.send_message(job.chat_id, f"🎬 <b>{html.escape(title)}</b>\n\n⏳ Starting…", reply_markup=cancel_kb)
    card = Card(msg, title, asyncio.get_running_loop(), cancel_kb)
    job.task = asyncio.create_task(_run(client, job, card, url, referer or config.M3U8_REFERER or url))


async def cancel(client, q, uid: int) -> None:
    job = BY_USER.get(uid)
    if not job or (q.from_user.id != uid and not is_owner(q.from_user.id)):
        await q.answer("Nothing to cancel.", show_alert=True)
        return
    job.stop()
    await q.answer("Cancelling…")


def _position(job: Job) -> tuple[int, int]:
    return (WAITING.index(job) + 1 if job in WAITING else 0), len(WAITING)


async def _run(client, job: Job, card: Card, url: str, referer: str) -> None:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(config.MAX_PARALLEL)
    trail = job.trail
    job_dir = os.path.join(config.DOWNLOAD_DIR, uuid.uuid4().hex[:10])
    try:
        WAITING.append(job)
        pos, total = _position(job)
        card.wait = f"⏳ Waiting for a free slot • position {pos} of {total}" if _sem.locked() else ""
        await card.push(force=True)
        async with _sem:
            if job in WAITING:
                WAITING.remove(job)
            job.state = "running"
            trail.state = "running"
            trail.add("slot acquired")
            await _pipeline(client, job, card, url, referer, job_dir)
        trail.end("done")
    except (asyncio.CancelledError, JobCancelled):
        trail.end("cancelled")
        card.final = "✖ Cancelled."
        await card.push(force=True)
    except Exception as exc:  # noqa: BLE001
        await _failed(client, job, card, exc)
    finally:
        if job in WAITING:
            WAITING.remove(job)
        BY_USER.pop(job.uid, None)
        shutil.rmtree(job_dir, ignore_errors=True)
        trail.add(f"cleaned up {job_dir}")


async def _failed(client, job: Job, card: Card, exc: BaseException) -> None:
    from pyrogram.errors import RPCError
    if isinstance(exc, JobError):
        public, detail = exc.public, exc.detail
    elif isinstance(exc, m3u8.DiskSpaceError):
        public, detail = "The server is out of disk space. Try again later.", str(exc)
    elif isinstance(exc, m3u8.ToolMissing):
        public, detail = "The server can't process videos right now.", str(exc)
    elif isinstance(exc, RPCError):
        public, detail = "Telegram refused the upload. Try again later.", f"{type(exc).__name__}: {exc}"
    else:
        public, detail = "Something went wrong.", f"{type(exc).__name__}: {exc}"
    job.trail.fail(exc, detail)
    job.trail.end("failed")
    dump_file = ""
    if job.dump is not None and job.dump.pages:  # keep the evidence: the pages the extractor opened
        try:
            dump_file = debuglog.save_dump(job.trail.ref, job.dump.zip_bytes(debuglog.scrub))
        except Exception:  # noqa: BLE001
            log.exception("could not save the extraction dump")
    log.error("[%s] %s: %s", job.trail.ref, job.title, debuglog.scrub(detail)[:500])
    text = f"❌ {html.escape(public)}\nReference <code>{job.trail.ref}</code>"
    if is_owner(job.uid):  # the owner sees why, right here
        text += f"\n\n<pre>{html.escape(debuglog.scrub(detail)[:700])}</pre>\nMore: /debug {job.trail.ref}"
    card.final = text
    await card.push(force=True)
    if config.OWNER_ID and not is_owner(job.uid):  # tell the owner, with the reference to look up
        try:
            await client.send_message(
                config.OWNER_ID,
                f"❌ <b>Download failed</b> • <code>{job.trail.ref}</code>\n🎬 {html.escape(job.title)}\n👤 {html.escape(job.trail.who)}\n"
                f"<pre>{html.escape(debuglog.scrub(detail)[:900])}</pre>\n/debug {job.trail.ref}",
                protect_content=True,
            )
            if dump_file:
                await client.send_document(config.OWNER_ID, dump_file, caption=f"Extraction dump for {job.trail.ref}: report.txt + every page",
                                           protect_content=True)
        except Exception:  # noqa: BLE001
            pass


async def _pipeline(client, job: Job, card: Card, url: str, referer: str, job_dir: str) -> None:
    trail = job.trail
    free = m3u8.free_mb(config.DOWNLOAD_DIR if os.path.exists(config.DOWNLOAD_DIR) else ".")
    trail.add(f"free disk: {free:.0f} MB (minimum {config.MIN_FREE_MB} MB)")
    if free < config.MIN_FREE_MB:
        raise m3u8.DiskSpaceError(f"Only {free:.0f} MB free, need at least {config.MIN_FREE_MB} MB.")

    # ---- 1. find the stream
    await card.begin("find", "looking at the player page…")
    t = time.monotonic()
    page_trace: list[str] = []
    alternates: list[str] = []
    job.dump = m3u8.Dump()

    def status(text: str):  # from the extractor, e.g. while the browser records the page
        card.detail = text
        card.push_threadsafe()

    if job.preset:  # /capture already found the stream
        m3u8_url, headers = job.preset["url"], job.preset["headers"]
        alternates = list(job.preset["alternates"])
        trail.add("stream chosen from a /capture result")
    else:
        m3u8_url, headers = await m3u8.extract_m3u8(url, referer=referer, trace=page_trace, alternates=alternates,
                                                    dump=job.dump, on_status=status)
    for line in page_trace:
        trail.add(f"extract: {line}")
    if not m3u8_url:
        raise JobError("Could not find a video for this title. It may not be available.",
                       "No stream found. The extraction dump (every page the bot opened) is attached to /debug "
                       + trail.ref + ".\n" + "\n".join(page_trace[:8]))
    trail.add(f"stream: {m3u8_url}  (+{len(alternates)} alternate url(s), {time.monotonic() - t:.1f}s)")
    await card.finish("find")

    # ---- 2. download (waits and continues when the host answers 403 / 429)
    await card.begin("download", "starting…")
    marks = {"next": 10}

    def on_dl(info: dict):  # worker thread
        job.check()
        if info.get("stage") == "finalize":
            card.detail = "joining the pieces…"
        else:
            card.detail = (f"{bar(info['pct'])} {info['pct']:.0f}% • {fmt_size(info['done'])} • {fmt_size(info['speed'])}/s "
                           f"• ETA {fmt_eta(info['eta'])}\n    {info['seg_done']}/{info['seg_total']} pieces • {info['workers']} parallel")
            if info["pct"] >= marks["next"]:
                trail.add(f"download {info['pct']:.0f}%  {fmt_size(info['done'])}  {fmt_size(info['speed'])}/s  workers={info['workers']}")
                marks["next"] = (int(info["pct"]) // 10 + 1) * 10
        card.push_threadsafe()

    resume: dict = {}
    path = ""
    for rnd in range(config.DL_ROUNDS):
        try:
            trail.add(f"download round {rnd + 1}/{config.DL_ROUNDS}")
            m3u8_url, path = await m3u8.download_any(
                [m3u8_url] + alternates[:12], headers, job_dir, job.title, progress=on_dl, check=job.check, resume=resume)
            break
        except m3u8.DownloadBlocked as exc:
            trail.add(f"blocked (HTTP 403/429): {str(exc)[:300]}")
            if rnd == config.DL_ROUNDS - 1:
                raise JobError("The video host is refusing downloads right now. Try again in a few minutes.",
                               f"Blocked in all {config.DL_ROUNDS} rounds.\n{exc}") from exc
            wait = config.DL_BLOCK_WAIT * (2 ** rnd) * random.uniform(0.85, 1.25)
            trail.add(f"waiting {wait:.0f}s, then a fresh stream link")
            end = time.monotonic() + wait
            while (left := end - time.monotonic()) > 0:
                card.wait = f"⏳ The video host is limiting requests - continuing in {fmt_eta(left)} (attempt {rnd + 2}/{config.DL_ROUNDS})"
                await card.push()
                job.check()
                await asyncio.sleep(min(2.0, left))
            card.wait = ""
            alternates = []
            fresh, fresh_headers = await m3u8.extract_m3u8(url, referer=referer, trace=[], alternates=alternates, on_status=status)
            trail.add("fresh stream link: " + ("yes" if fresh else "none, keeping the old one"))
            if fresh:
                m3u8_url, headers = fresh, fresh_headers
        except m3u8.SegmentFailure as exc:
            raise JobError("The video could not be downloaded.", str(exc)) from exc
        except RuntimeError as exc:
            if isinstance(exc, (m3u8.DiskSpaceError, m3u8.ToolMissing)):
                raise
            raise JobError("The video could not be downloaded.", str(exc)) from exc
    size = os.path.getsize(path)
    trail.add(f"downloaded {fmt_size(size)} -> {os.path.basename(path)}")
    await card.finish("download", fmt_size(size))

    # ---- 3. prepare: sound check, split under the Telegram limit
    await card.begin("prepare", "checking the file…")
    info = await m3u8.probe(path)
    trail.add(f"probe: {info}")
    note = ""
    if info["ok"] and not info["audio"]:
        trail.add("the video has NO audio track")
        if config.ADD_SILENT_AUDIO and await m3u8.add_silent_audio(path):
            note = "silent track added"
            trail.add("silent audio track added (Telegram would show a GIF)")
    parts = await m3u8.split_if_needed(path, config.MAX_UPLOAD_BYTES)
    sizes = ", ".join(fmt_size(os.path.getsize(p)) for p in parts)
    trail.add(f"parts: {len(parts)} ({sizes}); limit per part {fmt_size(config.MAX_UPLOAD_BYTES)}")
    await card.finish("prepare", (f"{len(parts)} parts" if len(parts) > 1 else "1 file") + (f" • {note}" if note else ""))

    # ---- 4. upload (each part is deleted after it was sent, so measure first)
    total = sum(os.path.getsize(p) for p in parts)
    await _upload(client, job, card, parts)
    card.done["upload"] = fmt_size(total)
    card.final = f"✅ Sent • {fmt_size(total)}" + (f" in {len(parts)} parts" if len(parts) > 1 else "")
    await card.push(force=True)


async def _upload(client, job: Job, card: Card, parts: list[str]) -> None:
    trail, n = job.trail, len(parts)
    for i, p in enumerate(parts, 1):
        job.check()
        info = await m3u8.probe(p)
        thumb = await m3u8.make_thumb(p, p + ".jpg", at=min(5, max(0, info["duration"] // 2)))
        size = os.path.getsize(p)
        label = f"part {i}/{n}" if n > 1 else "file"
        caption = f"🎬 {html.escape(job.title)}" + (f"\nPart {i}/{n}" if n > 1 else "")
        trail.add(f"upload {label}: {fmt_size(size)}, {info['width']}x{info['height']}, {info['duration']}s, thumb={'yes' if thumb else 'no'}")
        await card.begin("upload", f"{label}: starting…")
        t0, marks = time.monotonic(), {"next": 10}

        def on_up(cur: int, total: int):  # runs in a worker thread
            if job.cancel.is_set():
                client.stop_transmission()
            pct = 100 * cur / max(total, 1)
            speed = cur / max(time.monotonic() - t0, 0.001)
            eta = (total - cur) / speed if speed > 0 else 0
            card.detail = f"{label}: {bar(pct)} {pct:.0f}% • {fmt_size(cur)} / {fmt_size(total)} • {fmt_size(speed)}/s • ETA {fmt_eta(eta)}"
            if pct >= marks["next"]:
                trail.add(f"upload {label} {pct:.0f}%  {fmt_size(speed)}/s")
                marks["next"] = (int(pct) // 10 + 1) * 10
            card.push_threadsafe()

        name = os.path.basename(p)
        try:
            try:
                msg = await client.send_video(
                    job.chat_id, p, caption=caption[:1024], duration=info["duration"], width=info["width"], height=info["height"],
                    thumb=thumb, supports_streaming=True, file_name=name, progress=on_up)
            except Exception as exc:  # noqa: BLE001
                if job.cancel.is_set():
                    raise
                # Telegram sometimes refuses a file as a *video* (odd codec / container): a document always works
                trail.add(f"send_video failed ({type(exc).__name__}: {exc}); retrying {label} as a document")
                msg = await client.send_document(job.chat_id, p, caption=caption[:1024], thumb=thumb, file_name=name, progress=on_up)
        finally:
            if thumb and os.path.exists(thumb):
                os.remove(thumb)
        if msg is None:  # Pyrogram returns None when the transfer was stopped
            raise JobCancelled()
        trail.add(f"uploaded {label} in {time.monotonic() - t0:.0f}s (message {msg.id})")
        os.remove(p)  # free the disk for the next part


# ---------------------------------------------------------------- /probe
def _version(mod: str) -> str:
    try:
        m = __import__(mod)
        return getattr(m, "__version__", None) or getattr(getattr(m, "version", None), "__version__", None) or "installed"
    except Exception:  # noqa: BLE001
        return "NOT installed"


def environment() -> list[str]:
    t = m3u8.tools_status()
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    return [
        f"ffmpeg: {'bundled copy' if t['bundled'] else t['ffmpeg'] or 'MISSING (splitting, joining and thumbnails fail)'}",
        f"ffprobe: {t['ffprobe'] or 'missing (ffmpeg is used instead)'}",
        f"yt-dlp: {_version('yt_dlp')}   cryptography: {_version('cryptography')}   httpx: {_version('httpx')}",
        f"pyrogram: {_version('pyrogram')}   TgCrypto: {_version('tgcrypto')}"
        + ("" if _version("tgcrypto") != "NOT installed" else "  <- uploads are slow without it"),
        f"free disk: {m3u8.free_mb(config.DOWNLOAD_DIR):.0f} MB in {config.DOWNLOAD_DIR!r} (an HLS movie needs about 2x its size while it is joined)",
        f"upload limit per file: {fmt_size(config.MAX_UPLOAD_BYTES)} (bigger movies are split)   max height: {config.MAX_HEIGHT}p",
        f"parallel downloads: {config.MAX_PARALLEL}   segment workers: {config.DL_WORKERS}   upload parts: {config.MAX_TRANSMISSIONS}",
        f"proxy: {len(config.PROXIES)} configured   referer: {config.M3U8_REFERER or '-'}",
        f"browser capture: mode {config.CAPTURE_MODE}, "
        + ("playwright installed" if __import__("capture").available() else "playwright NOT installed (pip install playwright && playwright install chromium)")
        + f", watches up to {config.CAPTURE_SECONDS}s, ads blocked: {'yes' if config.CAPTURE_BLOCK_ADS else 'no'}",
        f"owner set: {'yes' if config.OWNER_ID else 'NO (/debug is off)'}   download users: {'everyone' if not config.DOWNLOAD_USERS else len(config.DOWNLOAD_USERS)}",
    ]


async def probe_title(url: str, referer: str = "") -> tuple[list[str], "m3u8.Dump"]:
    """Dry run: what would a download of this embed link do? Nothing is downloaded.
    -> (report lines, the extraction dump: send it with m3u8.Dump.zip_bytes())"""
    out = ["<b>Page</b>"]
    res = await m3u8.diagnose(url, referer=referer or None)
    out += [html.escape(l) for l in res["lines"]] or ["(no answer)"]
    trace: list[str] = []
    alternates: list[str] = []
    dump = m3u8.Dump()
    t = time.monotonic()
    found, hdr = await m3u8.extract_m3u8(url, referer=referer or url, trace=trace, alternates=alternates, dump=dump)
    out += ["", f"<b>Stream</b> ({time.monotonic() - t:.1f}s)"]
    if not found:
        out += ["❌ no stream found"] + [html.escape(x) for x in trace[:8]]
        out += ["", "The zip sent with this shows every page the bot opened and why each candidate was rejected."]
        return out, dump
    out += [f"▶️ <code>{html.escape(found)}</code>", f"alternates: {len(alternates)}"]
    out += [html.escape(x) for x in await m3u8.describe(found, hdr)]
    out += ["", "✅ A download would start from this stream."]
    return out, dump


async def probe_upload(client, chat_id: int, mb: int = 20) -> list[str]:
    """Upload a random file of `mb` MB to the chat and report speed and errors: checks the whole upload path."""
    mb = max(1, min(mb, 200))
    path = os.path.join(config.DOWNLOAD_DIR, f"probe_{uuid.uuid4().hex[:6]}.bin")
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    out = [f"<b>Upload test</b> ({mb} MB)"]
    try:
        with open(path, "wb") as f:
            for _ in range(mb):
                f.write(os.urandom(1024 * 1024))
        t0 = time.monotonic()
        seen = {"n": 0}

        def cb(cur, total):
            seen["n"] = cur

        msg = await client.send_document(chat_id, path, caption=f"upload test {mb} MB", file_name="upload_test.bin", progress=cb)
        dt = time.monotonic() - t0
        out.append(f"✅ sent in {dt:.1f}s = {fmt_size(mb * 1048576 / dt)}/s (message {msg.id})")
        out.append(f"a 2 GB file at this speed: about {fmt_eta(2048 / (mb / dt))}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"❌ {type(exc).__name__}: {html.escape(debuglog.scrub(exc))}")
        log.exception("upload test failed")
    finally:
        if os.path.exists(path):
            os.remove(path)
    return out
