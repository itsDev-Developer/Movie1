import asyncio
import html
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from pyrogram import Client, enums, filters, idle
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton as Btn
from pyrogram.types import InlineKeyboardMarkup as Markup
from pyrogram.types import WebAppInfo

load_dotenv()

import config  # noqa: E402  (after load_dotenv: reads the download / upload settings)
import debuglog  # noqa: E402
import capture  # noqa: E402
import downloader  # noqa: E402

debuglog.setup_logging()
log = logging.getLogger("moviebot")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")        # the API base url
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")    # public https url of this server
TMDB_KEY = os.getenv("TMDB_API_KEY")                 # optional: posters + overview
PORT = int(os.getenv("PORT", "8080"))



def valid_https(url: str) -> bool:
    """Telegram accepts a web-app button only for a public https:// address with a real domain."""
    p = urlsplit(url or "")
    host = (p.hostname or "").lower()
    return (p.scheme == "https" and "." in host and host != "localhost" and not re.fullmatch(r"[\d.]+", host)
            and not host.endswith(".example") and "your-domain" not in host)


def valid_http(url: str) -> bool:
    p = urlsplit(url or "")
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and "." in host and not host.endswith(".example")


WEBAPP_OK = valid_https(WEBAPP_URL)   # False: the "Open app" / "Watch in app" buttons are left out instead of failing
BASE_OK = valid_http(BASE_URL)


def setup_problems() -> list[str]:
    """Settings that make the bot misbehave: shown in the log at start, to the owner, and in /probe."""
    out = []
    if not WEBAPP_OK:
        out.append(f"WEBAPP_URL is {WEBAPP_URL!r}: it must be the public https:// address of this server (a real domain, no "
                   "localhost / IP / placeholder). Until then the web-app buttons are hidden.")
    if not BASE_OK:
        out.append(f"BASE_URL is {BASE_URL!r}: that is not the address of your movie API / embed site, so lists, details and downloads cannot work.")
    if not config.OWNER_ID:
        out.append("OWNER_ID is not set: /debug, /probe and /capture are off and nobody is told about errors.")
    return out


ID_RE = re.compile(r"^(tt\d+|\d+)$")
FEEDS = {"movie": "movie/new", "tv": "tv/new", "episode": "episode/latest"}
TITLES = {"movie": "Latest movies", "tv": "Latest shows", "episode": "Latest episodes"}

# ---------------------------------------------------------------- helpers

_session: aiohttp.ClientSession | None = None
_cache: dict = {}
_sem = asyncio.Semaphore(8)


async def get_json(url, params=None):
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    async with _session.get(url, params=params) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


def to_int(v, default=1):
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return default


def items_of(data):
    """The list endpoints may wrap results differently; accept the common shapes."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("result", "results", "data", "items"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def norm(it: dict, kind: str) -> dict:
    imdb = it.get("imdb_id") or it.get("imdb")
    tmdb = it.get("tmdb_id") or it.get("tmdb")
    return {
        "kind": kind,
        "id": str(imdb or tmdb or it.get("id") or ""),
        "title": it.get("title") or it.get("name") or it.get("show_title") or it.get("original_title") or "Untitled",
        "year": str(it.get("year") or (it.get("release_date") or "")[:4] or ""),
        "season": it.get("season"),
        "episode": it.get("episode"),
        "quality": it.get("quality"),
        "poster": None,
    }


def feed_url(kind, page):
    return f"{BASE_URL}/vapi/{FEEDS[kind]}" + (f"/{page}" if page > 1 else "")


def embed_url(kind, ident, season=1, ep=1, sub=None):
    if kind == "movie":
        url = f"{BASE_URL}/embed/movie/{ident}"
    else:
        url = f"{BASE_URL}/embed/tv/{ident}/{season}/{ep}"
    if sub:
        url += "?sub.info=" + quote(sub, safe="")
    return url


async def tmdb_details(kind, ident):
    if not TMDB_KEY:
        return None
    key = (kind, ident)
    if key in _cache:
        return _cache[key]
    t = "movie" if kind == "movie" else "tv"
    async with _sem:
        try:
            tid = ident
            if ident.startswith("tt"):
                f = await get_json(f"https://api.themoviedb.org/3/find/{ident}",
                                   {"api_key": TMDB_KEY, "external_source": "imdb_id"})
                res = f.get(f"{t}_results") or []
                if not res:
                    return None
                tid = res[0]["id"]
            d = await get_json(f"https://api.themoviedb.org/3/{t}/{tid}", {"api_key": TMDB_KEY})
        except Exception as e:
            log.warning("tmdb lookup failed for %s: %s", ident, e)
            return None
    p = d.get("poster_path")
    out = {
        "title": d.get("title") or d.get("name"),
        "overview": d.get("overview") or "",
        "poster": f"https://image.tmdb.org/t/p/w500{p}" if p else None,
        "rating": d.get("vote_average") or 0,
        "year": (d.get("release_date") or d.get("first_air_date") or "")[:4],
        "genres": [g["name"] for g in d.get("genres", [])],
        "runtime": d.get("runtime"),
        "seasons": d.get("number_of_seasons"),
    }
    _cache[key] = out
    return out


# ---------------------------------------------------------------- web (webapp + json proxy)

def check_id(v):
    if not v or not ID_RE.match(v):
        raise web.HTTPBadRequest(text="bad id")
    return v


async def api_list(req):
    kind = req.query.get("kind", "movie")
    if kind not in FEEDS:
        raise web.HTTPBadRequest(text="bad kind")
    page = to_int(req.query.get("page"))
    try:
        data = await get_json(feed_url(kind, page))
    except Exception as e:
        log.error("list failed: %s", e)
        return web.json_response({"items": [], "error": "upstream"}, status=502)
    k = "tv" if kind == "episode" else kind
    items = [i for i in (norm(x, k) for x in items_of(data)) if i["id"]]
    details = await asyncio.gather(*(tmdb_details(i["kind"], i["id"]) for i in items))
    for i, d in zip(items, details):
        if d:
            i["poster"] = d["poster"]
    return web.json_response({"items": items})


async def api_details(req):
    kind = req.query.get("kind", "movie")
    if kind not in ("movie", "tv"):
        raise web.HTTPBadRequest(text="bad kind")
    d = await tmdb_details(kind, check_id(req.query.get("id")))
    return web.json_response(d or {})


async def api_embed(req):
    kind = req.query.get("kind", "movie")
    if kind not in ("movie", "tv"):
        raise web.HTTPBadRequest(text="bad kind")
    url = embed_url(kind, check_id(req.query.get("id")), to_int(req.query.get("s")),
                    to_int(req.query.get("e")), req.query.get("sub") or None)
    return web.json_response({"url": url})


async def index(_):
    return web.FileResponse(Path(__file__).parent / "static" / "index.html",
                            headers={"Cache-Control": "no-cache"})


def make_web():
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/api/list", api_list),
        web.get("/api/details", api_details),
        web.get("/api/embed", api_embed),
    ])
    return app


# ---------------------------------------------------------------- bot

HELP = (
    "<b>Movies &amp; shows</b>\n\n"
    "Send an IMDb or TMDB id, or use:\n"
    "/movie <code>tt17048514</code>\n"
    "/tv <code>tt0944947 1 5</code> (id, season, episode)\n"
    "/new – latest movies\n"
    "/newtv – latest shows\n"
    "/episodes – latest episodes\n\n"
    "Tap <b>⬇️ Download</b> on a title to get the video file here (up to 2 GB per file; longer movies come in parts)."
)


def guarded(fn):
    """Any error in a command: the user gets a short message + a reference, the owner reads the rest with /debug."""
    async def wrapper(client, m):
        try:
            await fn(client, m)
        except Exception as exc:  # noqa: BLE001
            ref = debuglog.record_error(getattr(fn, "__name__", "handler"), exc, m.from_user)
            try:
                await m.reply_text(f"Something went wrong. Reference <code>{ref}</code>")
            except Exception:  # noqa: BLE001
                pass
    wrapper.__name__ = getattr(fn, "__name__", "handler")
    return wrapper


async def reply_safe(m, text: str, rows: list | None = None, photo: str | None = None, plain_rows: list | None = None):
    """Send a message that always arrives: with its poster and buttons if Telegram accepts them, else without the
    poster, else without the link / web-app buttons (`plain_rows` = the buttons that cannot be refused), else as bare text.
    The first refusal is recorded so the owner can read why with /debug."""
    attempts = []
    if photo:
        attempts.append(("photo", photo, rows))
    attempts += [("text", None, rows), ("text", None, plain_rows), ("text", None, None)]
    first_error = None
    for kind, ph, kb in attempts:
        try:
            markup = Markup(kb) if kb else None
            if kind == "photo":
                sent = await m.reply_photo(ph, caption=text[:1024], reply_markup=markup)
            else:
                sent = await m.reply_text(text, reply_markup=markup)
        except Exception as exc:  # noqa: BLE001
            first_error = first_error or exc
            log.warning("reply attempt failed (%s, %s): %s", kind, "buttons" if kb else "bare", debuglog.scrub(exc))
            continue
        if first_error:  # it worked in a simpler form: keep the reason the full form was refused
            debuglog.record_error("reply", first_error, getattr(m, "from_user", None), extra="(sent in a simpler form)")
        return sent
    debuglog.record_error("reply", first_error, getattr(m, "from_user", None))
    raise first_error


async def on_start(_, m):
    rows = [[Btn("🎬 Open app", web_app=WebAppInfo(url=WEBAPP_URL))]] if WEBAPP_OK else None
    await reply_safe(m, HELP, rows)


async def send_details(m, kind, ident, season=1, ep=1):
    d = await tmdb_details(kind, ident) or {}
    head = f"<b>{html.escape(d.get('title') or ident)}</b>"
    if d.get("year"):
        head += f" ({d['year']})"
    if kind == "tv":
        head += f" · S{season}E{ep}"
    lines = [head]
    meta = []
    if d.get("rating"):
        meta.append(f"⭐ {d['rating']:.1f}")
    if d.get("runtime"):
        meta.append(f"{d['runtime']} min")
    if d.get("seasons"):
        meta.append(f"{d['seasons']} seasons")
    if d.get("genres"):
        meta.append(", ".join(d["genres"][:3]))
    if meta:
        lines.append(" · ".join(meta))
    if d.get("overview"):
        lines.append("\n" + html.escape(d["overview"][:600]))
    text = "\n".join(lines)
    app_url = f"{WEBAPP_URL}/?" + urlencode({"kind": kind, "id": ident, "s": season, "e": ep})
    rows = []
    if WEBAPP_OK:
        rows.append([Btn("▶ Watch in app", web_app=WebAppInfo(url=app_url))])
    if BASE_OK:
        rows.append([Btn("🔗 Open embed link", url=embed_url(kind, ident, season, ep))])
    plain = []
    if config.DOWNLOADS:
        plain.append([Btn("⬇️ Download (up to 2 GB)", callback_data=f"dl:{kind}:{ident}:{season}:{ep}")])
    await reply_safe(m, text, rows + plain, d.get("poster"), plain)


async def on_movie(_, m):
    if len(m.command) < 2 or not ID_RE.match(m.command[1]):
        return await m.reply_text("Usage: <code>/movie tt17048514</code>")
    await send_details(m, "movie", m.command[1])


async def on_tv(_, m):
    a = m.command[1:]
    if not a or not ID_RE.match(a[0]):
        return await m.reply_text("Usage: <code>/tv tt0944947 1 5</code>")
    await send_details(m, "tv", a[0], to_int(a[1] if len(a) > 1 else 1), to_int(a[2] if len(a) > 2 else 1))


async def on_plain_id(_, m):
    await send_details(m, "movie", m.text.strip())


async def show_list(m, kind, page, edit=False):
    try:
        items = [norm(x, "tv" if kind == "episode" else kind) for x in items_of(await get_json(feed_url(kind, page)))]
    except Exception as e:
        ref = debuglog.record_error("list", e)
        return await m.reply_text(f"Couldn't load the list. Try again in a moment. Reference <code>{ref}</code>")
    items = [i for i in items if i["id"]][:10]
    rows = []
    for i in items:
        label = i["title"] + (f" ({i['year']})" if i["year"] else "")
        if kind == "episode":
            label += f" S{i['season']}E{i['episode']}"
        cb = f"dt:{i['kind']}:{i['id']}:{to_int(i['season'])}:{to_int(i['episode'])}"
        rows.append([Btn(label[:60], callback_data=cb)])
    nav = []
    if page > 1:
        nav.append(Btn("◀ Previous", callback_data=f"ls:{kind}:{page - 1}"))
    if items:
        nav.append(Btn("Next ▶", callback_data=f"ls:{kind}:{page + 1}"))
    if nav:
        rows.append(nav)
    text = f"<b>{TITLES[kind]}</b> · page {page}" if items else "Nothing more here."
    if edit:
        await m.edit_text(text, reply_markup=Markup(rows) if rows else None)
    else:
        await m.reply_text(text, reply_markup=Markup(rows) if rows else None)


def list_cmd(kind):
    async def h(_, m):
        await show_list(m, kind, 1)
    return h


async def start_download(client, q, kind, ident, season, ep):
    d = await tmdb_details(kind, ident) or {}
    title = d.get("title") or ident
    if d.get("year"):
        title += f" ({d['year']})"
    if kind == "tv":
        title += f" S{season:02d}E{ep:02d}"
    await downloader.start(client, q, title, embed_url(kind, ident, season, ep))


async def on_cb(client, q):
    p = q.data.split(":")
    try:
        if p[0] == "dl":  # ⬇️ Download
            await start_download(client, q, p[1], p[2], int(p[3]), int(p[4]))
            return
        if p[0] == "dp":  # ⬇️ a stream found by /capture
            await downloader.start_preset(client, q, p[1])
            return
        if p[0] == "cx":  # ✖ Cancel
            await downloader.cancel(client, q, int(p[1]))
            return
        if p[0] == "ls":
            await show_list(q.message, p[1], int(p[2]), edit=True)
        elif p[0] == "dt":
            await send_details(q.message, p[1], p[2], int(p[3]), int(p[4]))
    except Exception as e:
        ref = debuglog.record_error(f"button {p[0]}", e, q.from_user)
        await q.answer(f"Something went wrong. Reference {ref}", show_alert=True)
        return
    await q.answer()


# ---------------------------------------------------------------- owner: /debug and /probe
def _owner_ok(m) -> bool:
    """Only the owner sees errors and diagnostics. Everybody else gets no answer at all."""
    return bool(m.from_user) and downloader.is_owner(m.from_user.id)


async def send_long(m, text: str, as_html: bool = False, name: str = "report.txt"):
    """Short text as a message, long text as a file (nobody can forward it)."""
    if len(text) <= 3800:
        await m.reply_text(text if as_html else f"<pre>{html.escape(text)}</pre>", protect_content=True, disable_web_page_preview=True)
        return
    path = os.path.join(config.DOWNLOAD_DIR, name)
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    plain = re.sub(r"<[^>]+>", "", html.unescape(text)) if as_html else text
    with open(path, "w", encoding="utf-8") as f:
        f.write(plain)
    try:
        await m.reply_document(path, caption="Full report", protect_content=True)
    finally:
        os.remove(path)


async def on_debug(client, m):
    if not config.OWNER_ID:
        return await m.reply_text("Set OWNER_ID in the settings to use /debug and /probe.")
    if not _owner_ok(m):
        return
    arg = m.command[1] if len(m.command) > 1 else ""
    if arg.lower() == "log":
        if not os.path.exists(debuglog.LOG_FILE):
            return await m.reply_text("No log file yet.")
        return await m.reply_document(debuglog.LOG_FILE, caption="Latest log", protect_content=True)
    if not arg:
        return await send_long(m, debuglog.summary(), name="debug.txt")
    d = debuglog.find(arg)
    if not d:
        return await m.reply_text("No job or error with that reference. Send /debug for the list.")
    await send_long(m, debuglog.render(d), name=f"debug_{d['ref']}.txt")
    if zpath := debuglog.dump_path(d["ref"]):  # a failed extraction keeps every page it opened
        await m.reply_document(zpath, caption="Extraction dump: open report.txt first.", protect_content=True)


async def on_probe(client, m):
    """/probe                       tools, disk, limits
    /probe tt0111161             dry run for a movie: what the page returns and which stream is found
    /probe tt0944947 1 5         the same for a show episode (id season episode)
    /probe https://…/embed/…     the same for any embed link
    /probe upload [MB]           uploads a test file here: speed and errors of the upload path"""
    if not config.OWNER_ID:
        return await m.reply_text("Set OWNER_ID in the settings to use /debug and /probe.")
    if not _owner_ok(m):
        return
    a = m.command[1:]
    if not a:
        return await send_long(m, "<b>Environment</b>\n" + "\n".join(html.escape(x) for x in downloader.environment() + ["⚠️ " + p for p in setup_problems()])
                               + "\n\n" + html.escape(on_probe.__doc__), as_html=True)
    if a[0].lower() == "upload":
        note = await m.reply_text("Uploading a test file…")
        lines = await downloader.probe_upload(client, m.chat.id, to_int(a[1] if len(a) > 1 else 20))
        return await note.edit_text("\n".join(lines))
    if a[0].startswith("http"):
        url = a[0]
    elif ID_RE.match(a[0]):
        url = embed_url("tv" if len(a) > 1 else "movie", a[0], to_int(a[1] if len(a) > 1 else 1), to_int(a[2] if len(a) > 2 else 1))
    else:
        return await m.reply_text("Usage: <code>/probe tt0111161</code>, <code>/probe tt0944947 1 5</code>, a link, or <code>/probe upload 20</code>")
    note = await m.reply_text("Checking…")
    try:
        lines, dump = await downloader.probe_title(url, config.M3U8_REFERER)
    except Exception as exc:  # noqa: BLE001
        ref = debuglog.record_error("probe", exc, m.from_user)
        return await note.edit_text(f"Probe failed: <code>{html.escape(debuglog.scrub(exc))}</code>\n/debug {ref}")
    await note.delete()
    await send_long(m, f"<code>{html.escape(url)}</code>\n\n" + "\n".join(lines), as_html=True, name="probe.txt")
    # the evidence: report.txt (every request, answer and decision) + the raw text of every page / script / API answer
    zpath = debuglog.save_dump(debuglog.new_ref(), dump.zip_bytes(debuglog.scrub))
    await m.reply_document(zpath, caption=f"Extraction dump: {len(dump.pages)} page(s). Open report.txt first.", protect_content=True)


def _page_from_args(a: list[str]) -> tuple[str, str]:
    """`tt0111161` | `tt0944947 1 5` | a link  ->  (embed page url, a title for it). ('', '') when not understood."""
    if not a:
        return "", ""
    if a[0].startswith("http"):
        return a[0], urlsplit(a[0]).netloc
    if ID_RE.match(a[0]):
        kind = "tv" if len(a) > 1 else "movie"
        s, e = to_int(a[1] if len(a) > 1 else 1), to_int(a[2] if len(a) > 2 else 1)
        return embed_url(kind, a[0], s, e), a[0] + (f" S{s:02d}E{e:02d}" if kind == "tv" else "")
    return "", ""


async def on_capture(client, m):
    """/capture tt0111161 | /capture tt0944947 1 5 | /capture <link>
    Opens the embed page in a real browser, records EVERY request and response, finds the streams in that and
    sends the recording (requests table, HAR, response texts). Tap a button to download + upload a stream."""
    if not config.OWNER_ID:
        return await m.reply_text("Set OWNER_ID in the settings to use /capture.")
    if not _owner_ok(m):
        return
    url, title = _page_from_args(m.command[1:])
    if not url:
        return await m.reply_text("Usage: <code>/capture tt0111161</code>, <code>/capture tt0944947 1 5</code> or <code>/capture &lt;link&gt;</code>\n"
                                  + html.escape(on_capture.__doc__.split("\n", 1)[1]))
    if not capture.available():
        return await m.reply_text("Browser capture is not installed on this server:\n<code>pip install playwright\nplaywright install chromium</code>")
    note = await m.reply_text("🌐 Recording the page in a browser… (up to ~%ds)" % config.CAPTURE_SECONDS)
    last = {"t": 0.0}

    def status(text: str):  # called from the recorder
        if time.monotonic() - last["t"] > 4:
            last["t"] = time.monotonic()
            asyncio.ensure_future(note.edit_text(f"🌐 {html.escape(text)}"))
    try:
        cap, streams, good = await capture.run(url, config.M3U8_REFERER, status)
    except Exception as exc:  # noqa: BLE001
        ref = debuglog.record_error("capture", exc, m.from_user)
        return await note.edit_text(f"Capture failed: <code>{html.escape(debuglog.scrub(exc))}</code>\n/debug {ref}")
    working = {s.url: h for s, h in good}
    lines = [f"<code>{html.escape(url)}</code>", f"📡 {len(cap.entries)} requests ({html.escape(cap.counts())}) in {cap.finished - cap.started:.0f}s", ""]
    rows = []
    for i, s in enumerate(streams[:6], 1):
        ok = "✅" if s.url in working else "✖"
        lines.append(f"{i}. {ok} <b>{s.kind.upper()}</b> score {s.score:g} • {'requested' if s.observed else 'mentioned'}\n   <code>{html.escape(s.url[:300])}</code>\n   {html.escape('; '.join(dict.fromkeys(s.why)))}")
    for s, h in good[:3]:
        token = downloader.remember_stream(title, url, s.url, h, [x.url for x, _ in good if x.url != s.url and x.kind == s.kind])
        rows.append([Btn(f"⬇️ Download #{streams.index(s) + 1} ({s.kind})", callback_data=f"dp:{token}")])
    if not streams:
        lines.append("No stream in any request or response. The zip shows everything the page did.")
    elif not good:
        lines.append("\nCandidates were found but none could be downloaded (see report.txt).")
    await note.delete()
    await m.reply_text("\n".join(lines)[:4000], reply_markup=Markup(rows) if rows else None, protect_content=True, disable_web_page_preview=True)
    zpath = debuglog.save_dump("CAP" + debuglog.new_ref()[:3], cap.zip_bytes(streams, debuglog.scrub))
    await m.reply_document(zpath, caption="Recording: report.txt, requests.txt, capture.har (open in Chrome dev-tools → Network → Import), bodies/",
                           protect_content=True)


async def main():
    debuglog.load_history()
    extra = dict(sleep_threshold=60, max_concurrent_transmissions=config.MAX_TRANSMISSIONS, workers=config.TG_WORKERS)
    try:
        bot = Client("moviebot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
                     in_memory=True, parse_mode=enums.ParseMode.HTML, **extra)
    except TypeError:  # an older Pyrogram without max_concurrent_transmissions
        extra.pop("max_concurrent_transmissions")
        bot = Client("moviebot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
                     in_memory=True, parse_mode=enums.ParseMode.HTML, **extra)
    bot.add_handler(MessageHandler(guarded(on_start), filters.command(["start", "help"])))
    bot.add_handler(MessageHandler(guarded(on_movie), filters.command("movie")))
    bot.add_handler(MessageHandler(guarded(on_tv), filters.command("tv")))
    bot.add_handler(MessageHandler(guarded(list_cmd("movie")), filters.command("new")))
    bot.add_handler(MessageHandler(guarded(list_cmd("tv")), filters.command("newtv")))
    bot.add_handler(MessageHandler(guarded(list_cmd("episode")), filters.command("episodes")))
    bot.add_handler(MessageHandler(guarded(on_debug), filters.command(["debug", "diagnose"])))
    bot.add_handler(MessageHandler(guarded(on_probe), filters.command("probe")))
    bot.add_handler(MessageHandler(guarded(on_capture), filters.command("capture")))
    bot.add_handler(MessageHandler(guarded(on_plain_id), filters.private & filters.regex(ID_RE.pattern)))
    bot.add_handler(CallbackQueryHandler(on_cb))

    runner = web.AppRunner(make_web())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await bot.start()
    log.info("running on :%s, webapp at %s", PORT, WEBAPP_URL)
    problems = setup_problems()
    for p in problems:
        log.warning("SETTINGS: %s", p)
    if problems and config.OWNER_ID:
        try:
            await bot.send_message(config.OWNER_ID, "⚠️ <b>Settings to fix</b>\n" + "\n".join(f"• {html.escape(p)}" for p in problems),
                                   protect_content=True)
        except Exception:  # noqa: BLE001  (the owner has not opened the bot yet)
            pass
    await idle()
    await bot.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
