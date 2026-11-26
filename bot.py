"""
ViralX AI Editor & Universal Link Downloader  (Step 1 + 2)

Webhook-only Telegram bot for Render.
Features: English button menu, video download, Shorts creation (9:16 clips),
editor settings. Publishing / captions arrive in later steps.
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from fastapi import FastAPI, Request, Response
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("viralx")

# ---------------------------------------------------------------- config
BOT_TOKEN = (
    os.environ.get("BOT_TOKEN")
    or os.environ.get("TELEGRAM_BOT_TOKEN")
    or os.environ.get("TELEGRAM_TOKEN")
    or os.environ.get("TOKEN")
)
if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Render > Environment")

BASE_URL = os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
OUT_W = int(os.environ.get("OUT_W", "720"))     # free plan is slow: 720x1280
OUT_H = int(os.environ.get("OUT_H", "1280"))    # set 1080 / 1920 on a paid plan
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

MAX_TG_UPLOAD = 49 * 1024 * 1024     # bots can send up to 50 MB
MAX_TG_DOWNLOAD = 20 * 1024 * 1024   # bots can download up to 20 MB
URL_RE = re.compile(r"https?://\S+")
START_TIME = time.time()
JOB_LOCK = asyncio.Semaphore(1)      # one heavy job at a time (512 MB RAM)

# Optional YouTube cookies (fixes "Sign in to confirm you're not a bot")
COOKIE_FILE = None
for src_cookie in ("/etc/secrets/cookies.txt", None):
    if src_cookie and Path(src_cookie).exists():
        shutil.copy(src_cookie, "/tmp/yt_cookies.txt")
        COOKIE_FILE = "/tmp/yt_cookies.txt"
if not COOKIE_FILE and os.environ.get("YT_COOKIES"):
    Path("/tmp/yt_cookies.txt").write_text(os.environ["YT_COOKIES"])
    COOKIE_FILE = "/tmp/yt_cookies.txt"

MENU_TEXT = (
    "👋 Welcome to ViralX AI Editor!\n\n"
    "Paste a video link (YouTube, TikTok, Instagram, Facebook, X...) "
    "or upload a video, and I will download it or turn it into Shorts.\n\n"
    "Choose an option:"
)


# ---------------------------------------------------------------- keyboards
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download Video", callback_data="mode:download")],
        [InlineKeyboardButton("✂️ Create Shorts", callback_data="mode:shorts")],
        [InlineKeyboardButton("🎛 Editor Settings", callback_data="settings")],
        [InlineKeyboardButton("🤖 Autopilot", callback_data="autopilot"),
         InlineKeyboardButton("📤 Publish", callback_data="publish")],
        [InlineKeyboardButton("📊 Status", callback_data="status")],
    ])


def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to menu", callback_data="menu")]])


def settings_kb(s):
    def mark(ok, label):
        return ("✅ " if ok else "") + label

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(mark(s["length"] == n, f"{n}s"), callback_data=f"len:{n}") for n in (15, 30, 45, 60)],
        [InlineKeyboardButton(mark(s["count"] == n, f"{n} clips"), callback_data=f"cnt:{n}") for n in (1, 2, 3, 5)],
        [InlineKeyboardButton(mark(s["style"] == "fill", "Fill frame"), callback_data="style:fill"),
         InlineKeyboardButton(mark(s["style"] == "blur", "Blur background"), callback_data="style:blur")],
        [InlineKeyboardButton("⬅️ Back to menu", callback_data="menu")],
    ])


def get_settings(ud):
    return ud.setdefault("settings", {"length": 30, "count": 3, "style": "fill"})


# ---------------------------------------------------------------- media helpers (blocking)
def friendly_error(e):
    m = str(e)
    if "Sign in to confirm" in m or "429" in m:
        return ("YouTube blocked this server (bot check). Add YouTube cookies in Render "
                "(YT_COOKIES), or upload the video file directly (max 20 MB).")
    if "Private video" in m or "login" in m.lower():
        return "This video is private or needs a login."
    if "Unsupported URL" in m:
        return "This link is not supported."
    return "Download failed: " + m[:300]


def ydl_download(url, outdir, max_height=720):
    opts = {
        "outtmpl": str(Path(outdir) / "%(id)s.%(ext)s"),
        "format": f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG,
        "retries": 3,
        "socket_timeout": 30,
    }
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    rd = info.get("requested_downloads")
    if rd and rd[0].get("filepath"):
        path = rd[0]["filepath"]
    p = Path(path)
    if not p.exists():
        alt = p.with_suffix(".mp4")
        if not alt.exists():
            raise FileNotFoundError("Downloaded file not found")
        p = alt
    return str(p), (info.get("title") or "video")


def video_duration(path):
    r = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def loudness_profile(path):
    """Momentary loudness over time. Louder = more exciting moment."""
    p = subprocess.run(
        [FFMPEG, "-nostats", "-i", path, "-vn", "-af", "ebur128=framelog=verbose", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    pts = []
    for line in p.stderr.splitlines():
        m = re.search(r"\bt:\s*([\d.]+).*?\bM:\s*(-?(?:\d+\.?\d*|inf))", line)
        if m:
            pts.append((float(m.group(1)), max(float(m.group(2)), -120.0)))
    return pts


def pick_windows(pts, duration, length, count):
    length = int(length)
    if duration <= length + 1:
        return [(0.0, max(1.0, duration))]
    last_start = int(duration) - length
    if not pts:  # no audio: spread evenly
        step = max(1, last_start // max(1, count))
        return [(float(i * step), float(length)) for i in range(count) if i * step <= last_start]
    n = int(duration)
    buckets = [[] for _ in range(n + 1)]
    for t, v in pts:
        i = int(t)
        if i <= n:
            buckets[i].append(v)
    energy = [(sum(b) / len(b)) if b else -120.0 for b in buckets]
    pre = [0.0]
    for e in energy:
        pre.append(pre[-1] + e)
    cands = [((pre[s + length] - pre[s]) / length, s) for s in range(0, last_start + 1)]
    cands.sort(reverse=True)
    chosen = []
    for _, s in cands:
        if all(abs(s - c) >= length for c in chosen):
            chosen.append(s)
        if len(chosen) == count:
            break
    chosen.sort()
    return [(float(s), float(length)) for s in chosen]


def make_clip(src, start, length, dest, style):
    W, H = OUT_W, OUT_H
    base = [FFMPEG, "-y", "-ss", str(start), "-t", str(length), "-i", src]
    if style == "blur":
        fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:5[bg];"
              f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v]")
        vid = ["-filter_complex", fc, "-map", "[v]", "-map", "0:a?"]
    else:
        vid = ["-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"]
    enc = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "27", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", dest]
    subprocess.run(base + vid + enc, check=True, capture_output=True, timeout=1800)


# ---------------------------------------------------------------- jobs
async def run_job(bot, chat_id, ud, action, url=None, file_id=None):
    s = get_settings(ud)
    if JOB_LOCK.locked():
        await bot.send_message(chat_id, "⏳ Another job is running. Yours will start right after it.")
    async with JOB_LOCK:
        status = await bot.send_message(chat_id, "⏬ Getting your video...")
        tmp = tempfile.mkdtemp(prefix="vx_")

        async def say(text):
            try:
                await status.edit_text(text)
            except BadRequest:
                pass

        try:
            if file_id:
                f = await bot.get_file(file_id)
                src = str(Path(tmp) / "input.mp4")
                await f.download_to_drive(src)
                title = "Uploaded video"
            else:
                src, title = await asyncio.to_thread(ydl_download, url, tmp)

            if action == "download":
                size = os.path.getsize(src)
                if size > MAX_TG_UPLOAD:
                    await say(f"⚠️ Video is {size // 1048576} MB. Telegram bots can send max 50 MB. "
                              "Use ✂️ Create Shorts instead.")
                else:
                    await say("📤 Sending video...")
                    with open(src, "rb") as fh:
                        await bot.send_video(chat_id, video=fh, caption=title[:200], supports_streaming=True,
                                             read_timeout=300, write_timeout=300, connect_timeout=60)
                    await say("✅ Done!")
            else:
                dur = await asyncio.to_thread(video_duration, src)
                await say("🔎 Finding the best moments...")
                pts = await asyncio.to_thread(loudness_profile, src)
                windows = pick_windows(pts, dur, s["length"], s["count"])
                sent = 0
                for i, (st, ln) in enumerate(windows, 1):
                    await say(f"🎬 Rendering Short {i}/{len(windows)}...")
                    dest = str(Path(tmp) / f"short_{i}.mp4")
                    await asyncio.to_thread(make_clip, src, st, ln, dest, s["style"])
                    if os.path.getsize(dest) > MAX_TG_UPLOAD:
                        await bot.send_message(chat_id, f"⚠️ Short {i} is over 50 MB, skipped.")
                        continue
                    with open(dest, "rb") as fh:
                        await bot.send_video(chat_id, video=fh, width=OUT_W, height=OUT_H, duration=int(ln),
                                             caption=f"Short {i}/{len(windows)} • from {int(st)}s",
                                             supports_streaming=True,
                                             read_timeout=300, write_timeout=300, connect_timeout=60)
                    sent += 1
                await say(f"✅ Done! {sent} Short(s) sent.")
        except Exception as e:  # noqa: BLE001
            log.exception("job failed")
            await say("❌ " + friendly_error(e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    await bot.send_message(chat_id, "What next?", reply_markup=main_menu())


# ---------------------------------------------------------------- handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = None
    await update.effective_message.reply_text(MENU_TEXT, reply_markup=main_menu())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    ud = context.user_data
    s = get_settings(ud)

    async def edit(text, kb):
        try:
            await q.message.edit_text(text, reply_markup=kb)
        except BadRequest:
            pass

    settings_text = ("🎛 Editor Settings\n\nClip length, clips per video and framing.\n"
                     "Captions, music and branding come in the next step.")

    if d == "menu":
        ud["mode"] = None
        await edit(MENU_TEXT, main_menu())
    elif d.startswith("mode:"):
        ud["mode"] = d.split(":")[1]
        if ud["mode"] == "download":
            await edit("📥 Download Video\n\nSend me a video link.", back_kb())
        else:
            await edit(f"✂️ Create Shorts\n\nSend a video link, or upload a video (max 20 MB).\n"
                       f"Current: {s['count']} clips × {s['length']}s.", back_kb())
    elif d == "settings":
        await edit(settings_text, settings_kb(s))
    elif d.startswith("len:"):
        s["length"] = int(d.split(":")[1])
        await edit(settings_text, settings_kb(s))
    elif d.startswith("cnt:"):
        s["count"] = int(d.split(":")[1])
        await edit(settings_text, settings_kb(s))
    elif d.startswith("style:"):
        s["style"] = d.split(":")[1]
        await edit(settings_text, settings_kb(s))
    elif d == "autopilot":
        ud["autopilot"] = not ud.get("autopilot", False)
        await edit(f"🤖 Autopilot: {'ON' if ud['autopilot'] else 'OFF'}\n\n"
                   "⚠️ Auto-publishing is not connected yet (Step 3). For now this switch only saves "
                   "your choice. Nothing is posted automatically.", back_kb())
    elif d == "publish":
        await edit("📤 Publish\n\nPosting to YouTube Shorts, TikTok and Facebook Reels comes in Step 3 "
                   "and needs your official API keys. Until then, Shorts are sent to you in this chat.",
                   back_kb())
    elif d == "status":
        up = int(time.time() - START_TIME)
        await edit(
            "📊 Status\n\n"
            f"Uptime: {up // 3600}h {(up % 3600) // 60}m\n"
            f"Output: {OUT_W}x{OUT_H}\n"
            f"YouTube cookies: {'loaded' if COOKIE_FILE else 'not set'}\n"
            f"Autopilot: {'ON' if ud.get('autopilot') else 'OFF'} (publishing not connected)\n"
            f"Settings: {s['count']} clips × {s['length']}s, {s['style']}",
            back_kb())
    elif d.startswith("go:"):
        action = d.split(":")[1]
        url = ud.pop("pending_url", None)
        if not url:
            await edit("Send the link again, please.", back_kb())
            return
        await edit("Starting...", None)
        await run_job(context.bot, q.message.chat_id, ud, action, url=url)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    m = URL_RE.search(msg.text or "")
    if not m:
        await msg.reply_text("Send me a video link, or tap /start for the menu.")
        return
    url = m.group(0)
    ud = context.user_data
    mode = ud.get("mode")
    if mode in ("download", "shorts"):
        await run_job(context.bot, msg.chat_id, ud, mode, url=url)
    else:
        ud["pending_url"] = url
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Download", callback_data="go:download"),
            InlineKeyboardButton("✂️ Make Shorts", callback_data="go:shorts"),
        ]])
        await msg.reply_text("What should I do with this link?", reply_markup=kb)


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    v = msg.video or msg.document
    if v.file_size and v.file_size > MAX_TG_DOWNLOAD:
        await msg.reply_text("⚠️ Telegram lets bots download files up to 20 MB only. "
                             "Please send a link instead, or compress the video.")
        return
    await run_job(context.bot, msg.chat_id, context.user_data, "shorts", file_id=v.file_id)


# ---------------------------------------------------------------- app
tg_app = Application.builder().token(BOT_TOKEN).updater(None).concurrent_updates(True).build()
tg_app.add_handler(CommandHandler(["start", "menu"], start))
tg_app.add_handler(CallbackQueryHandler(on_button))
tg_app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg_app.initialize()
    await tg_app.start()
    if BASE_URL:
        await tg_app.bot.set_webhook(
            url=f"{BASE_URL.rstrip('/')}/webhook",
            allowed_updates=Update.ALL_TYPES,
            secret_token=WEBHOOK_SECRET or None,
        )
        log.info("Webhook set to %s/webhook", BASE_URL.rstrip("/"))
    yield
    await tg_app.stop()
    await tg_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "ok", "bot": "ViralX AI Editor"}


@app.post("/webhook")
async def webhook(request: Request):
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return Response(status_code=403)
    update = Update.de_json(await request.json(), tg_app.bot)
    await tg_app.update_queue.put(update)
    return Response(status_code=200)
