"""Everything that goes wrong (or right) while downloading and uploading, kept for the owner.

* every download job writes a *trail*: each step with the time since the start, the stream host, the engine's own
  warnings (403 / 429 / retries...), upload progress, and on failure the full error with its traceback;
* errors outside a job (a button, a list, a lookup) are recorded too;
* users only ever see a reference code. `/debug` (owner only) lists the latest jobs and errors, `/debug <ref>`
  shows one in full, `/debug log` sends the log file.
Everything stored is scrubbed of the bot token, API hash and signed tokens first."""
import json
import logging
import logging.handlers
import os
import re
import secrets
import threading
import time
import traceback
from collections import deque

import config

BOT_SECRETS = [os.getenv(k, "") for k in ("BOT_TOKEN", "API_HASH", "TMDB_API_KEY")]
_PATTERNS = [
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "‹bot-token›"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{12,}"), r"\1‹hidden›"),
    (re.compile(r"(?i)\b((?:set-)?cookie['\"]?\s*[:=]\s*['\"]?)[^\r\n]+"), r"\1‹hidden›"),
    (re.compile(r"(?i)([?&;](?:token|sig|signature|key|apikey|api_key|api-key|auth|password|secret|hash|policy|credential)=)[^&\s\"'<>]+"), r"\1***"),
]
LOG_FILE = os.path.join("logs", "bot.log")
JOBS_FILE = os.path.join("logs", "jobs.jsonl")
DUMP_DIR = os.path.join("logs", "dumps")


def scrub(text) -> str:
    s = str(text if text is not None else "")
    for v in BOT_SECRETS:
        if v and len(v) >= 6 and v in s:
            s = s.replace(v, "‹hidden›")
    for rx, rep in _PATTERNS:
        s = rx.sub(rep, s)
    return s


def new_ref() -> str:
    return secrets.token_hex(3).upper()


class ScrubFormatter(logging.Formatter):
    def format(self, record):
        return scrub(super().format(record))


# ---------------------------------------------------------------- the trail of one job
class Trail:
    MAX_LINES = 500

    def __init__(self, title: str, user, kind: str = "download"):
        self.ref = new_ref()
        self.title, self.kind = title, kind
        self.uid = getattr(user, "id", 0)
        self.who = f"{getattr(user, 'first_name', '') or ''} ({self.uid})".strip()
        self.started = time.time()
        self.finished = 0.0
        self.state = "queued"
        self.error = ""        # one line
        self.trace = ""        # full traceback
        self.lines: list[str] = []
        self.lock = threading.Lock()

    def add(self, text: str) -> None:
        line = f"+{time.time() - self.started:7.1f}s  {scrub(text)}"
        with self.lock:
            if len(self.lines) < self.MAX_LINES:
                self.lines.append(line)
            elif len(self.lines) == self.MAX_LINES:
                self.lines.append("... (more lines dropped)")

    def fail(self, exc: BaseException, detail: str = "") -> None:
        self.state = "failed"
        self.error = scrub(f"{type(exc).__name__}: {detail or exc}")[:600]
        self.trace = scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[-6000:]
        self.add(f"FAILED  {self.error}")

    def end(self, state: str) -> None:
        self.state, self.finished = state, time.time()
        self.add(f"end: {state}")
        _finish(self)

    def as_dict(self) -> dict:
        return {"ref": self.ref, "title": self.title, "kind": self.kind, "who": self.who, "state": self.state,
                "started": self.started, "finished": self.finished, "error": self.error, "trace": self.trace,
                "lines": self.lines}

    def text(self, full: bool = True) -> str:
        head = (f"{self.ref}  {self.kind}  {self.state}\n{self.title}\nby {self.who}\n"
                f"started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))}"
                + (f", took {self.finished - self.started:.0f}s" if self.finished else ", still running") + "\n")
        body = "\n".join(self.lines if full else self.lines[-12:])
        tail = f"\n\n--- error ---\n{self.error}\n\n--- traceback ---\n{self.trace}" if full and self.error else ""
        return f"{head}\n{body}{tail}"


ACTIVE: list[Trail] = []
RECENT: deque = deque(maxlen=60)      # finished trails as dicts, newest first
ERRORS: deque = deque(maxlen=60)      # errors outside a job: {"ref","where","who","error","trace","at"}
_lock = threading.Lock()


def begin(title: str, user, kind: str = "download") -> Trail:
    t = Trail(title, user, kind)
    with _lock:
        ACTIVE.append(t)
    return t


def _finish(t: Trail) -> None:
    with _lock:
        if t in ACTIVE:
            ACTIVE.remove(t)
        RECENT.appendleft(t.as_dict())
    try:
        os.makedirs("logs", exist_ok=True)
        with open(JOBS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(t.as_dict(), ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_history() -> None:
    """Bring the last jobs back after a restart."""
    try:
        with open(JOBS_FILE, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f.readlines()[-RECENT.maxlen:] if x.strip()]
    except (OSError, ValueError):
        return
    for r in rows:
        RECENT.appendleft(r)


def record_error(where: str, exc: BaseException, user=None, extra: str = "") -> str:
    """An error outside a download job. Returns the reference the user is shown."""
    ref = new_ref()
    ERRORS.appendleft({
        "ref": ref, "where": where, "at": time.time(),
        "who": f"{getattr(user, 'first_name', '') or ''} ({getattr(user, 'id', 0)})" if user else "-",
        "error": scrub(f"{type(exc).__name__}: {exc} {extra}".strip())[:600],
        "trace": scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))[-6000:],
    })
    logging.getLogger("moviebot").error("[%s] %s: %s", ref, where, scrub(f"{type(exc).__name__}: {exc}"))
    return ref


def find(ref: str):
    ref = ref.strip().upper()
    with _lock:
        for t in ACTIVE:
            if t.ref == ref:
                return t.as_dict()
    for d in RECENT:
        if d["ref"] == ref:
            return d
    for e in ERRORS:
        if e["ref"] == ref:
            return e
    return None


def render(d: dict) -> str:
    if "lines" in d:
        t = Trail.__new__(Trail)
        t.__dict__.update(d)
        t.lock = threading.Lock()
        return t.text(full=True)
    return (f"{d['ref']}  error in {d['where']}\nwhen {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(d['at']))}  by {d['who']}\n\n"
            f"{d['error']}\n\n--- traceback ---\n{d['trace']}")


_ICON = {"done": "✅", "failed": "❌", "cancelled": "✖", "queued": "⏳", "running": "▶️"}


def summary(limit: int = 10) -> str:
    with _lock:
        live = [t.as_dict() for t in ACTIVE]
    rows = []
    for d in live + list(RECENT)[:limit]:
        icon = _ICON.get(d["state"], "▶️")
        err = f" - {d['error'][:70]}" if d.get("error") else ""
        rows.append(f"{icon} {d['ref']}  {d['title'][:34]}{err}")
    errs = [f"⚠️ {e['ref']}  {e['where']}: {e['error'][:70]}" for e in list(ERRORS)[:6]]
    out = ["Jobs (newest first):"] + (rows or ["none yet"])
    if errs:
        out += ["", "Other errors:"] + errs
    out += ["", "/debug REF - everything about one job or error", "/debug log - the log file", "/probe - test a title or the upload"]
    return "\n".join(out)


# ---------------------------------------------------------------- logging
class _TrailHandler(logging.Handler):
    """The downloader engine logs what it does (403 retries, mirrors, fallbacks): copy that into the running jobs."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("pyrogram") and record.levelno < logging.WARNING:
            return
        try:
            line = f"[{record.levelname}] {record.name}: {record.getMessage()}"
        except Exception:  # noqa: BLE001
            return
        with _lock:
            active = list(ACTIVE)
        for t in active:
            t.add(line)


def setup_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    fmt = ScrubFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    fileh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fileh.setFormatter(fmt)
    try:
        os.chmod(LOG_FILE, 0o600)
    except OSError:
        pass
    root.handlers = [console, fileh, _TrailHandler(level=logging.INFO)]
    for noisy in ("pyrogram", "httpx", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------- extraction dumps (zip of every page the extractor saw)
def save_dump(ref: str, data: bytes, keep: int = 30) -> str:
    """Store the zip for a job / probe; the oldest are removed so the disk never fills up."""
    os.makedirs(DUMP_DIR, exist_ok=True)
    path = os.path.join(DUMP_DIR, f"{ref}.zip")
    with open(path, "wb") as f:
        f.write(data)
    files = sorted((os.path.join(DUMP_DIR, x) for x in os.listdir(DUMP_DIR)), key=os.path.getmtime)
    for old in files[:-keep]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


def dump_path(ref: str) -> str:
    path = os.path.join(DUMP_DIR, f"{re.sub(r'[^A-Za-z0-9]', '', ref).upper()}.zip")
    return path if os.path.exists(path) else ""
