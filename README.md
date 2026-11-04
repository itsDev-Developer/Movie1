# moviebot

Browse movies / shows, open them in the web app, and **download them as a video file in Telegram (up to 2 GB per file)**.

## Download and upload

Tap **⬇️ Download** on a title. The bot finds the stream on the embed page, downloads it (HLS, direct files, yt-dlp / ffmpeg as fall-backs),
makes sure it has sound, **splits it under 2 GB if needed** (stream copy, so no quality loss) and uploads every part as a streamable video
(a document if Telegram refuses it as a video). A progress card shows each step, speed and ETA, with a ✖ Cancel button.
One download per person; `MAX_PARALLEL` run at once, the rest wait in line. If the stream host answers 403 / 429 the bot slows down,
rotates headers / proxies, waits, fetches a fresh stream link and continues where it stopped.

Needs `ffmpeg` (or `imageio-ffmpeg`, already in requirements on Linux x86_64) and `tgcrypto` (fast uploads). An HLS movie needs about
twice its size in free disk while it is joined.

## Finding out what went wrong (owner only)

Users only see a short sentence and a reference such as `Reference 3FA9C1`. Set `OWNER_ID` and you get the details:

| Command | Does |
|---|---|
| `/debug` | latest jobs and errors with their references |
| `/debug 3FA9C1` | everything about one job: each step with timings, stream host, 403 / 429 retries, upload progress, the error and its traceback |
| `/debug log` | the log file |
| `/probe` | tools (ffmpeg, yt-dlp, TgCrypto), free disk, limits |
| `/probe tt0111161` · `/probe tt0944947 1 5` · `/probe <link>` | dry run: what the page returns, which stream is found, nothing downloaded |
| `/probe upload 20` | uploads a 20 MB test file here: speed, and the error if the upload path is broken |

When a user's download fails you also get a message with the reference. Tokens, hashes and signed URLs are scrubbed from everything stored.
## "Could not find a video" - the extraction dump

`/probe tt0111161` (or a link) and every failed download send you a **zip**: `report.txt` lists each page, script and API answer the bot
opened (status, redirects, size, server), every iframe / candidate it followed and why each was rejected; `pages/` holds the raw text of
each one. Open `report.txt` first. It also says when the page is a Cloudflare / bot-protection screen. The extractor follows iframes,
`src:` / `.attr("src")` / `location=` / meta-refresh hops, external scripts, base64 / packed JS, API calls, playlist URLs without `.m3u8`
and `{v1}` host templates. For protected pages install `curl_cffi` (automatic Chrome fingerprint) or turn on `CAPTURE_MODE=fallback`.

## Browser capture: every request and response of the embed page

Many players build the stream address in JavaScript (API call → token → playlist), so it is in no HTML or script. With Playwright
installed the bot opens the page in a real headless Chromium like a viewer would, presses play, and **records every request and every
response** (URLs, headers, cookies, status, timing, and the text of every page / script / API answer / playlist). It then finds the
stream in that recording:

* playlists the player requested (recognised by name, content type, or by starting with `#EXTM3U`), video files, and URLs only
  *mentioned* inside API JSON;
* ranked: long VOD master playlist > big video file > live stream > previews under 2 minutes > ads (ad / tracker hosts are also blocked);
* each candidate is really tried with the headers and cookies the browser used (Referer, Origin, session cookie) before it is chosen;
* the cookie is only ever sent to the stream's own host, never to a redirect target.

It runs automatically when the normal extraction finds nothing (`CAPTURE_MODE=fallback`, the default when Playwright is installed), or first
with `CAPTURE_MODE=first`. Failed jobs and `/probe` zips then contain `capture/…` too.

**`/capture tt0111161`** · `/capture tt0944947 1 5` · `/capture <embed link>` (owner): records the page, replies with the streams it found
(✅ downloadable / ✖ not, score, why) with **⬇️ Download** buttons that download and upload that exact stream, and sends the recording as a
zip: `report.txt`, `requests.txt` (every request: status, type, size, ms), **`capture.har`** (open in Chrome dev-tools → Network → ⬆ Import),
`streams.txt` (ranking with the headers used), `bodies/` (response texts). Cookie values are hidden in everything exported.

Install: `pip install playwright && playwright install chromium` (Docker: `playwright install --with-deps chromium`). On low-memory hosts it is
off unless you set `CAPTURE_MODE`.

Tests: `python tests/test_capture.py` (needs Chromium), `python tests/test_extract.py`, `python tests/test_pipeline.py`, `python tests/test_blocking.py` (offline, need ffmpeg).

## "Something went wrong. Reference XXXXXX" on /start

Every command is wrapped so an error never goes silent: the user sees that sentence and a reference, and the full reason is stored.
Find it with `/debug XXXXXX` (owner) or by searching `logs/bot.log` / the console for `[XXXXXX]`.
The usual cause on /start is **`WEBAPP_URL`**: Telegram only accepts a web-app button for a public `https://` address with a real domain
(not `http://`, `localhost`, an IP, or the `your-domain.example` placeholder). The bot now checks `WEBAPP_URL` / `BASE_URL` / `OWNER_ID`
when it starts (log + a message to the owner + `/probe`), hides buttons Telegram would refuse, and still answers.
