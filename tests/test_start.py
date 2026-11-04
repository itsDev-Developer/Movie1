"""/start and the title pages must answer even when Telegram refuses the buttons or the poster (bad WEBAPP_URL etc.).

    python tests/test_start.py
"""
import asyncio
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.update(API_ID="1", API_HASH="h" * 20, BOT_TOKEN="1:" + "a" * 35, BASE_URL="https://baseurl", WEBAPP_URL="https://your-domain.example",
                  OWNER_ID="", CAPTURE_MODE="off")
os.chdir(tempfile.mkdtemp())


class Btn:
    def __init__(self, text, **kw): self.text, self.kw = text, kw
class Markup:
    def __init__(self, rows): self.rows = rows
class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return _Any()
    def __getattr__(self, n): return _Any()
for n in ("pyrogram", "pyrogram.types", "pyrogram.handlers", "pyrogram.errors", "aiohttp", "aiohttp.web", "httpx", "yt_dlp"):
    sys.modules[n] = types.ModuleType(n)
    sys.modules[n].__getattr__ = lambda name: _Any
sys.modules["pyrogram"].filters = _Any()
sys.modules["pyrogram.types"].InlineKeyboardButton = Btn
sys.modules["pyrogram.types"].InlineKeyboardMarkup = Markup
sys.modules["pyrogram.types"].WebAppInfo = lambda url: ("webapp", url)
sys.modules["aiohttp"].web = sys.modules["aiohttp.web"]

import bot  # noqa: E402
import debuglog  # noqa: E402

fails = 0
def check(label, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + label + (f"  [{extra}]" if extra else ""))
    fails += 0 if cond else 1


check("placeholder / http / localhost / IP / no-domain addresses are not valid web-app addresses",
      not any(bot.valid_https(u) for u in ("https://your-domain.example", "http://bot.example.com", "https://localhost:8080", "https://10.0.0.5", "https://baseurl", "", "bot.example.com")))
check("a real https address is valid", bot.valid_https("https://movies.koyeb.app") and bot.valid_http("http://api.site.org"))
check("the settings check names the placeholder WEBAPP_URL / BASE_URL and the missing OWNER_ID",
      sum("WEBAPP_URL" in p for p in bot.setup_problems()) == 1 and any("BASE_URL" in p for p in bot.setup_problems()) and any("OWNER_ID" in p for p in bot.setup_problems()))


class Reject(Exception): pass

class Msg:
    """A Telegram that refuses web-app / url buttons and posters, like BUTTON_URL_INVALID / WEBPAGE_MEDIA_EMPTY."""
    def __init__(self, refuse_buttons=True, refuse_photo=True):
        self.sent, self.refuse_buttons, self.refuse_photo = [], refuse_buttons, refuse_photo
        self.from_user = types.SimpleNamespace(id=5, first_name="Sam")
    def _check(self, markup):
        if self.refuse_buttons and markup:
            for row in markup.rows:
                for b in row:
                    if "web_app" in b.kw or "url" in b.kw:
                        raise Reject("BUTTON_URL_INVALID")
    async def reply_text(self, text, reply_markup=None, **k):
        self._check(reply_markup); self.sent.append(("text", text, reply_markup))
    async def reply_photo(self, photo, caption="", reply_markup=None, **k):
        if self.refuse_photo:
            raise Reject("WEBPAGE_MEDIA_EMPTY")
        self._check(reply_markup); self.sent.append(("photo", caption, reply_markup))


async def main():
    # /start with the placeholder WEBAPP_URL: no web-app button is even tried
    m = Msg()
    await bot.guarded(bot.on_start)(None, m)
    check("/start answers with the help text when WEBAPP_URL is the placeholder", len(m.sent) == 1 and "Movies" in m.sent[0][1] and m.sent[0][2] is None)

    # the same, but pretend the URL looked valid and Telegram refuses it anyway
    bot.WEBAPP_OK = True
    m = Msg()
    await bot.guarded(bot.on_start)(None, m)
    check("/start still answers when Telegram refuses the button (no 'Something went wrong')", len(m.sent) == 1 and "Something went wrong" not in m.sent[0][1] and "Movies" in m.sent[0][1])
    check("   and the real reason was recorded for the owner", any("BUTTON_URL_INVALID" in e["error"] for e in debuglog.ERRORS) and debuglog.ERRORS[0]["where"] == "reply")
    bot.WEBAPP_OK = False

    # a title page with poster + url buttons + the Download button
    async def details(*a, **k): return {"title": "Fake", "year": 2024, "poster": "https://image.tmdb.org/x.jpg", "overview": "x"}
    bot.tmdb_details = details
    bot.BASE_OK, bot.WEBAPP_OK = True, True
    m = Msg(refuse_buttons=True, refuse_photo=True)
    await bot.guarded(lambda c, mm: bot.send_details(mm, "movie", "tt1"))(None, m)
    kinds = [b.kw for row in m.sent[0][2].rows for b in row] if m.sent and m.sent[0][2] else []
    check("title page: poster and link buttons refused -> text with the Download button still arrives", len(m.sent) == 1 and m.sent[0][0] == "text" and any("callback_data" in k for k in kinds) and not any("url" in k or "web_app" in k for k in kinds))
    m = Msg(refuse_buttons=False, refuse_photo=False)
    await bot.guarded(lambda c, mm: bot.send_details(mm, "movie", "tt1"))(None, m)
    check("title page: when Telegram accepts everything it is the full version (poster + app + embed + download)", m.sent[0][0] == "photo" and len([b for row in m.sent[0][2].rows for b in row]) == 3)

    print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
    return 1 if fails else 0

sys.exit(asyncio.run(main()))
