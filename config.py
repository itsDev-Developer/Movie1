"""Settings for downloading / uploading (read from the environment / .env). bot.py keeps its own required ones."""
import os
import re

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _ids(name: str) -> set[int]:
    return {int(x) for x in re.split(r"[,\s]+", os.getenv(name, "")) if x.strip().lstrip("-").isdigit()}


BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OWNER_ID = _int("OWNER_ID")                  # may use /debug and /probe, and always may download. 0 = those commands are off
DOWNLOAD_USERS = _ids("DOWNLOAD_USERS")      # empty = everybody may use the Download button; else only these (+ the owner)
DOWNLOADS = _int("DOWNLOADS", 1) != 0        # 0 hides the Download button

KOYEB = bool(os.getenv("KOYEB_APP_NAME") or os.getenv("KOYEB_SERVICE_NAME"))
LITE = _int("LOW_MEMORY", 1 if KOYEB else 0) != 0   # small hosts: fewer parallel downloads / upload parts

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
MAX_PARALLEL = max(1, _int("MAX_PARALLEL", 1 if LITE else 2))          # downloads running at once (the rest wait)
MAX_HEIGHT = _int("MAX_HEIGHT", 720 if LITE else 1080)                 # best quality to fetch (HLS)
# Telegram lets a bot upload 2 GB per file; stay a little under it. Bigger movies are split into parts.
MAX_UPLOAD_BYTES = min(_int("MAX_UPLOAD_MB", 1950), 2000) * 1024 * 1024
ADD_SILENT_AUDIO = _int("ADD_SILENT_AUDIO", 1) != 0                    # a video without sound would show as a GIF
MIN_FREE_MB = _int("MIN_FREE_MB", 300)                                 # refuse to start with less free disk than this

# ---- the downloader engine (m3u8.py)
DL_WORKERS = max(1, min(_int("DL_WORKERS", 3 if LITE else 6), 16))
DL_CHUNK_MB = max(1, min(_int("DL_CHUNK_MB", 2 if LITE else 4), 64))
DL_WINDOW = max(1, min(_int("DL_WINDOW", 2 if LITE else 4), 8))
PROXY = os.getenv("PROXY", "").strip()
PROXIES = list(dict.fromkeys(p for p in re.split(r"[,\s]+", PROXY + "," + os.getenv("PROXY_LIST", "")) if p))
M3U8_REFERER = os.getenv("M3U8_REFERER", (BASE_URL + "/") if BASE_URL else "").strip()
SOURCES: dict = {}  # extra sites whose root is tried as Referer (the engine reads this; none here)
# host answers 403 / 429: pacing, patience, and rounds of "wait + fresh stream link + continue"
DL_MIN_INTERVAL_MS = max(0, _int("DL_MIN_INTERVAL_MS", 200 if LITE else 120))
BLOCK_ATTEMPTS = max(4, _int("BLOCK_ATTEMPTS", 14))
DL_ROUNDS = max(1, min(_int("DL_ROUNDS", 4), 8))
DL_BLOCK_WAIT = max(5, _int("DL_BLOCK_WAIT", 45))

# ---- Pyrogram (every upload part is buffered in memory)
MAX_TRANSMISSIONS = max(1, min(_int("MAX_TRANSMISSIONS", 2 if LITE else 4), 8))
TG_WORKERS = max(1, _int("TG_WORKERS", 2 if LITE else 4))

# ---- Browser capture (capture.py): open the embed page in headless Chromium, record every request / response and
# find the stream in that. Needs: pip install playwright && playwright install chromium
#   off       never        fallback  when the page itself shows no stream (default)        first  before anything else
_mode = os.getenv("CAPTURE_MODE", "").strip().lower() or ("fallback" if _int("BROWSER_FALLBACK", 1) else "off")
CAPTURE_MODE = _mode if _mode in ("off", "fallback", "first") else "fallback"
if LITE and not os.getenv("CAPTURE_MODE"):
    CAPTURE_MODE = "off"                                          # Chromium needs a few hundred MB: opt in on small hosts
CAPTURE_SECONDS = max(8, min(_int("CAPTURE_SECONDS", 40), 180))    # how long to watch the page (stops early once a stream is seen)
CAPTURE_BLOCK_ADS = _int("CAPTURE_BLOCK_ADS", 1) != 0              # abort requests to known ad / tracker hosts
CAPTURE_BLOCK_ASSETS = _int("CAPTURE_BLOCK_ASSETS", 1) != 0        # abort images and fonts (faster, nothing to learn there)
CAPTURE_BODY_MB = max(1, _int("CAPTURE_BODY_MB", 6))               # response text kept in the recording
CAPTURE_CHROMIUM = os.getenv("CAPTURE_CHROMIUM", "").strip()       # path of a system Chromium, if Playwright's own is not installed
