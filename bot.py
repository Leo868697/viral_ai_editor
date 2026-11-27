"""
VIRALX AI STUDIO
Version 1.1 - Phase 1
Telegram Universal Downloader + Video to Shorts
FastAPI + Telegram Webhook + yt-dlp + FFmpeg
"""

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from fastapi import FastAPI, Request, Response
import imageio_ffmpeg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import yt_dlp

# =====================================================
# CONFIGURATION
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# httpx logs full request URLs at INFO level, and Telegram URLs contain
# the bot token. Keep these quiet so the token never appears in logs.
for noisy in ("httpx", "httpcore", "telegram.ext.ExtBot"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("viralx")

BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TOKEN")
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in Render Environment")

BASE_URL = (
    os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
).rstrip("/")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# =====================================================
# TELEGRAM APPLICATION
# =====================================================

tg = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

OUT_W = 720
OUT_H = 1280

MAX_TG_DOWNLOAD = 20 * 1024 * 1024
MAX_TG_UPLOAD = 49 * 1024 * 1024
MAX_SOURCE_SIZE = 500 * 1024 * 1024
MAX_DURATION = 10 * 60 * 60

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".avi")

URL_PATTERN = re.compile(r"https?://[^\s]+")
START_TIME = time.time()

JOB_LOCK = asyncio.Semaphore(1)

# Keeps references to background jobs so they are not garbage collected.
BACKGROUND_TASKS = set()


def spawn(coroutine):
    """Run a long job in the background so the webhook returns instantly."""
    task = asyncio.create_task(coroutine)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task


class FileTooBig(Exception):
    pass


# =====================================================
# USER SETTINGS
# =====================================================

DEFAULT_SETTINGS = {
    "length": 30,
    "count": 3,
    "style": "fill",
}


def get_settings(user_data):
    if "settings" not in user_data:
        user_data["settings"] = DEFAULT_SETTINGS.copy()
    return user_data["settings"]


# =====================================================
# TELEGRAM MENUS
# =====================================================

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download Video", callback_data="mode:download")],
        [InlineKeyboardButton("✂️ Video to Shorts", callback_data="mode:shorts")],
        [InlineKeyboardButton("📡 Recording to Shorts", callback_data="mode:live")],
        [InlineKeyboardButton("🎛 Editor Settings", callback_data="settings")],
        [
            InlineKeyboardButton("🤖 Autopilot", callback_data="autopilot"),
            InlineKeyboardButton("📤 Publish", callback_data="publish"),
        ],
        [InlineKeyboardButton("📊 System Status", callback_data="status")],
    ])


def back_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")]
    ])


def choose_action_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Download", callback_data="act:download"),
            InlineKeyboardButton("✂️ Make Shorts", callback_data="act:shorts"),
        ],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")],
    ])


def settings_menu(s):
    lengths = [15, 30, 45, 60]
    counts = [1, 2, 3, 5]

    keyboard = [
        [
            InlineKeyboardButton(
                ("✅ " if s["length"] == n else "") + f"{n}s",
                callback_data=f"len:{n}",
            )
            for n in lengths
        ],
        [
            InlineKeyboardButton(
                ("✅ " if s["count"] == n else "") + f"{n} Clips",
                callback_data=f"count:{n}",
            )
            for n in counts
        ],
        [
            InlineKeyboardButton(
                ("✅ " if s["style"] == "fill" else "") + "Fill",
                callback_data="style:fill",
            ),
            InlineKeyboardButton(
                ("✅ " if s["style"] == "blur" else "") + "Blur",
                callback_data="style:blur",
            ),
        ],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu")],
    ]

    return InlineKeyboardMarkup(keyboard)


# =====================================================
# UNIVERSAL DOWNLOADER (yt-dlp)
# Works with YouTube, Instagram, TikTok, Facebook, X/Twitter,
# Reddit, Vimeo, direct video file links and 1000+ other sites.
# =====================================================

def _download_once(url, folder, height):
    fmt = (
        f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
        f"b[height<={height}][ext=mp4]/"
        f"bv*[height<={height}]+ba/"
        f"b[height<={height}]/"
        f"best"
    )

    options = {
        "outtmpl": str(Path(folder) / "dl.%(ext)s"),
        "format": fmt,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "max_filesize": MAX_SOURCE_SIZE,
        "restrictfilenames": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    if info is None:
        raise ValueError("The link did not return any video information.")

    if info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if entries:
            info = entries[0]

    candidates = [
        p for p in Path(folder).glob("dl.*")
        if p.suffix.lower() in VIDEO_EXTENSIONS
    ]

    if not candidates:
        raise ValueError(
            "No video could be downloaded. The post may contain only "
            "images, or the video exceeds the 500 MB server limit."
        )

    path = max(candidates, key=lambda p: p.stat().st_size)

    if path.stat().st_size > MAX_SOURCE_SIZE:
        raise ValueError("Video exceeds the configured size limit.")

    return path, info.get("title") or "Video"


def _clear_downloads(folder):
    for old in Path(folder).glob("dl.*"):
        try:
            old.unlink()
        except OSError:
            pass


def download_video(url, folder, heights=(1080,), fit_limit=None):
    """
    Download a video. When fit_limit is set, quality is lowered step by
    step until the file fits under that size (used for Telegram upload).
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError("Only HTTP and HTTPS links are accepted.")

    last_error = None

    for height in heights:
        _clear_downloads(folder)

        try:
            path, title = _download_once(url, folder, height)
        except Exception as error:
            last_error = error
            # Only try a lower quality when the format was the problem.
            if "format" in str(error).lower() and height != heights[-1]:
                continue
            raise

        if fit_limit is None or path.stat().st_size <= fit_limit:
            return str(path), title

        last_error = FileTooBig(
            "File is too big for Telegram even at the lowest quality."
        )

    raise last_error or FileTooBig("Video could not be downloaded.")


# =====================================================
# VIDEO INFORMATION
# =====================================================

def get_duration(path):
    result = subprocess.run(
        [FFMPEG, "-i", path],
        capture_output=True,
        text=True,
        timeout=60,
    )

    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
        result.stderr,
    )

    if not match:
        raise ValueError("Unable to read video duration.")

    hours, minutes, seconds = match.groups()

    return (
        int(hours) * 3600
        + int(minutes) * 60
        + float(seconds)
    )


# =====================================================
# AUDIO ANALYSIS
# Finds louder moments as a basic highlight signal.
# This is not semantic AI highlight detection.
# =====================================================

def detect_audio_peaks(path):
    result = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i", path,
            "-vn",
            "-af", "ebur128=framelog=verbose",
            "-f", "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )

    points = []

    for line in result.stderr.splitlines():
        match = re.search(
            r"\bt:\s*([\d.]+).*?\bM:\s*(-?\d+(?:\.\d+)?)",
            line,
        )

        if match:
            points.append((
                float(match.group(1)),
                float(match.group(2)),
            ))

    return points


def select_clips(points, duration, length, count):
    length = min(int(length), int(duration))

    if duration <= length:
        return [(0.0, max(1.0, duration))]

    last_start = int(duration - length)

    if not points:
        step = max(1, last_start // max(count, 1))
        return [
            (float(i * step), float(length))
            for i in range(count)
            if i * step <= last_start
        ]

    buckets = [[] for _ in range(int(duration) + 1)]

    for timestamp, loudness in points:
        index = int(timestamp)
        if 0 <= index < len(buckets):
            buckets[index].append(loudness)

    energy = [
        sum(bucket) / len(bucket) if bucket else -120.0
        for bucket in buckets
    ]

    prefix = [0.0]
    for value in energy:
        prefix.append(prefix[-1] + value)

    candidates = []

    for start in range(last_start + 1):
        end = min(start + length, len(energy))
        score = (prefix[end] - prefix[start]) / max(length, 1)
        candidates.append((score, start))

    candidates.sort(reverse=True)

    selected = []

    for score, start in candidates:
        if all(
            abs(start - previous) >= length
            for previous in selected
        ):
            selected.append(start)

        if len(selected) >= count:
            break

    selected.sort()

    return [
        (float(start), float(length))
        for start in selected
    ]


# =====================================================
# SHORTS RENDER ENGINE
# =====================================================

def render_short(source, start, length, destination, style):
    base = [
        FFMPEG,
        "-y",
        "-ss", str(start),
        "-i", source,
        "-t", str(length),
    ]

    if style == "blur":
        video_filter = (
            f"[0:v]scale={OUT_W}:{OUT_H}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{OUT_H},boxblur=20:5[bg];"
            f"[0:v]scale={OUT_W}:{OUT_H}:"
            f"force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
        )

        video_options = [
            "-filter_complex", video_filter,
            "-map", "[v]",
            "-map", "0:a?",
        ]
    else:
        video_options = [
            "-vf",
            f"scale={OUT_W}:{OUT_H}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{OUT_H}",
        ]

    encoding = [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "27",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        destination,
    ]

    subprocess.run(
        base + video_options + encoding,
        check=True,
        capture_output=True,
        timeout=1800,
    )


# =====================================================
# ERROR MESSAGES
# =====================================================

def readable_error(error):
    message = str(error)

    if isinstance(error, FileTooBig):
        return (
            "This video is too big to send through Telegram, even at the "
            "lowest quality. Try a shorter video."
        )

    if "Sign in to confirm" in message or "429" in message:
        return (
            "YouTube refused this server request. "
            "Try an authorized video upload or another supported source."
        )

    if "Unsupported URL" in message:
        return "This link is not supported by the downloader."

    if "Private video" in message or "login" in message.lower():
        return "This video is private or requires authorization."

    if isinstance(error, subprocess.CalledProcessError):
        return "Video rendering failed. The file may be damaged or unsupported."

    return "Processing failed: " + message[:250]


# =====================================================
# MAIN PROCESSING JOB
# =====================================================

async def process_job(
    bot,
    chat_id,
    user_data,
    action,
    url=None,
    file_id=None,
):
    s = get_settings(user_data)

    status = await bot.send_message(
        chat_id,
        "⏳ Your job is waiting...",
    )

    temp_folder = tempfile.mkdtemp(prefix="viralx_")

    async def update_status(message):
        try:
            await status.edit_text(message)
        except TelegramError:
            pass

    try:
        async with JOB_LOCK:
            await update_status("📥 Receiving your video...")

            if file_id:
                telegram_file = await bot.get_file(file_id)

                source = str(
                    Path(temp_folder) / "uploaded_video.mp4"
                )

                await telegram_file.download_to_drive(
                    custom_path=source
                )

                title = "Uploaded Video"

            else:
                await update_status(
                    "🌐 Downloading from supported source..."
                )

                if action == "download":
                    heights = (1080, 720, 480, 360, 240)
                    fit_limit = MAX_TG_UPLOAD
                else:
                    heights = (1080,)
                    fit_limit = None

                source, title = await asyncio.to_thread(
                    download_video,
                    url,
                    temp_folder,
                    heights,
                    fit_limit,
                )

            duration = await asyncio.to_thread(
                get_duration,
                source,
            )

            if duration <= 0 or duration > MAX_DURATION:
                raise ValueError(
                    "Video must be between 1 second and 10 hours."
                )

            if action == "download":
                await update_status("📤 Sending your video...")

                with open(source, "rb") as video:
                    await bot.send_video(
                        chat_id,
                        video=video,
                        caption=title[:200],
                        supports_streaming=True,
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=60,
                    )

                await update_status("✅ Download complete!")

            else:
                await update_status(
                    "🔎 Analysing audio and finding clip moments..."
                )

                points = await asyncio.to_thread(
                    detect_audio_peaks,
                    source,
                )

                windows = select_clips(
                    points,
                    duration,
                    s["length"],
                    s["count"],
                )

                completed = 0

                for index, (start, clip_length) in enumerate(
                    windows, 1
                ):
                    await update_status(
                        f"🎬 Rendering Short {index}/{len(windows)}..."
                    )

                    destination = str(
                        Path(temp_folder) / f"short_{index}.mp4"
                    )

                    await asyncio.to_thread(
                        render_short,
                        source,
                        start,
                        clip_length,
                        destination,
                        s["style"],
                    )

                    if os.path.getsize(destination) > MAX_TG_UPLOAD:
                        await bot.send_message(
                            chat_id,
                            f"⚠️ Short {index} is too large to send.",
                        )
                        continue

                    with open(destination, "rb") as video:
                        await bot.send_video(
                            chat_id,
                            video=video,
                            width=OUT_W,
                            height=OUT_H,
                            duration=int(clip_length),
                            caption=(
                                f"✂️ ViralX Short {index}\n"
                                f"Duration: {int(clip_length)}s\n"
                                f"Source position: {int(start)}s"
                            ),
                            supports_streaming=True,
                            read_timeout=300,
                            write_timeout=300,
                            connect_timeout=60,
                        )

                    completed += 1

                await update_status(
                    f"✅ Completed! {completed} Short(s) created."
                )

    except Exception as error:
        log.exception("Video processing failed")
        await update_status("❌ " + readable_error(error))

    finally:
        shutil.rmtree(temp_folder, ignore_errors=True)

    try:
        await bot.send_message(
            chat_id,
            "What would you like to do next?",
            reply_markup=main_menu(),
        )
    except TelegramError:
        pass


# =====================================================
# TELEGRAM COMMANDS
# =====================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = None

    await update.effective_message.reply_text(
        "🚀 WELCOME TO VIRALX AI STUDIO\n\n"
        "Your video downloader and Shorts creation workspace.\n\n"
        "Choose a service below, or simply send a video link:",
        reply_markup=main_menu(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = None
    context.user_data.pop("pending_url", None)

    await update.effective_message.reply_text(
        "Current selection cleared.",
        reply_markup=main_menu(),
    )


# =====================================================
# BUTTON HANDLER
# =====================================================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Old buttons (pressed after a restart) make Telegram return
    # "Query is too old". That must never break the handler.
    try:
        await query.answer()
    
        pass

    data = query.data or ""
    s = get_settings(context.user_data)

    if data == "menu":
        await query.edit_message_text(
            "Choose a service:",
            reply_markup=main_menu()
        )

    elif data == "mode:download":
        context.user_data["mode"] = "download"
        await query.edit_message_text(
            "📥 Send your supported download link.\n"
            "YouTube is disabled."
        )

    elif data == "mode:shorts":
        context.user_data["mode"] = "shorts"
        await query.edit_message_text(
            "✂️ Send a video link or upload a video."
        )

    elif data == "settings":
        await query.edit_message_text(
            "🎛 EDITOR SETTINGS",
            reply_markup=settings_menu(s)
        )

    elif data.startswith("length:"):
        s["length"] = int(data.split(":")[1])
        await query.edit_message_reply_markup(
            reply_markup=settings_menu(s)
        )

    elif data.startswith("count:"):
        s["count"] = int(data.split(":")[1])
        await query.edit_message_reply_markup(
            reply_markup=settings_menu(s)
        )

    elif data.startswith("style:"):
        s["style"] = data.split(":")[1]
        await query.edit_message_reply_markup(
            reply_markup=settings_menu(s)
        )

    elif data == "status":
        await query.edit_message_text(
            "🟢 VIRALX ONLINE\n"
            f"Source limit: {MAX_SOURCE_MB} MB\n"
            f"Telegram delivery limit: {MAX_TG_UPLOAD_MB} MB",
            reply_markup=main_menu()
        )


def get_duration(path):
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=60
    )

    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
        result.stderr
    )

    if not match:
        raise ValueError("Cannot read video duration.")

    h, m, sec = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(sec)


def choose_windows(duration, length, count):
    length = min(float(length), duration)

    if duration <= length:
        return [(0.0, duration)]

    if count == 1:
        return [((duration - length) / 2, length)]

    return [
        (
            i * (duration - length) / (count - 1),
            length
        )
        for i in range(count)
    ]


def render_short(source, start, length, output, style):
    if style == "fill":
        vf = (
            "scale=720:1280:"
            "force_original_aspect_ratio=increase,"
            "crop=720:1280"
        )
        options = ["-vf", vf]

    else:
        vf = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=720:1280:"
            "force_original_aspect_ratio=increase,"
            "crop=720:1280,boxblur=22:8[blur];"
            "[fg]scale=720:1280:"
            "force_original_aspect_ratio=decrease[front];"
            "[blur][front]overlay=(W-w)/2:(H-h)/2[v]"
        )
        options = [
            "-filter_complex", vf,
            "-map", "[v]",
            "-map", "0:a?"
        ]

    command = [
        FFMPEG, "-y",
        "-ss", str(start),
        "-i", str(source),
        "-t", str(length)
    ] + options + [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "27",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output)
    ]

    subprocess.run(
        command,
        check=True,
        capture_output=True,
        timeout=1800
    )


async def process_job(
    application,
    chat_id,
    user_data,
    url=None,
    file_id=None,
    action="download"
):
    bot = application.bot

    status = await bot.send_message(
        chat_id,
        "⏳ Your job is waiting..."
    )

    folder = Path(tempfile.mkdtemp(prefix="viralx_"))

    try:
        async with JOB_LOCK:

            if file_id:
                await status.edit_text(
                    "📥 Receiving uploaded video..."
                )

                telegram_file = await bot.get_file(file_id)

                source = folder / "uploaded_video.mp4"

                await telegram_file.download_to_drive(
                    custom_path=str(source)
                )

                title = "Uploaded Video"

            else:
                await status.edit_text(
                    "🌐 Downloading your file..."
                )

                source, title, info = await asyncio.to_thread(
                    download_any,
                    url,
                    folder,
                    MAX_SOURCE_MB * 1024 * 1024
                )

            size = source.stat().st_size

            if size > MAX_SOURCE_MB * 1024 * 1024:
                raise ValueError(
                    "Source file exceeds configured limit."
                )

            if action == "download":

                if size > MAX_TG_UPLOAD_MB * 1024 * 1024:
                    raise ValueError(
                        "File exceeds Telegram delivery limit."
                    )

                await status.edit_text(
                    "📤 Sending your file..."
                )

                with source.open("rb") as file:
                    await bot.send_document(
                        chat_id,
                        document=file,
                        filename=source.name,
                        caption=str(title)[:900]
                    )

                await status.edit_text(
                    "✅ Download complete!"
                )

            else:
                await status.edit_text(
                    "🎬 Preparing Shorts..."
                )

                duration = await asyncio.to_thread(
                    get_duration,
                    source
                )

                if duration < 1 or duration > MAX_DURATION:
                    raise ValueError(
                        "Video must be 1 second to 10 hours."
                    )

                s = user_data.get(
                    "settings",
                    DEFAULT_SETTINGS
                )

                windows = choose_windows(
                    duration,
                    s["length"],
                    s["count"]
                )

                completed = 0

                for index, (start, length) in enumerate(
                    windows, 1
                ):
                    await status.edit_text(
                        f"🎬 Rendering Short {index}/{len(windows)}..."
                    )

                    output = folder / f"short_{index}.mp4"

                    await asyncio.to_thread(
                        render_short,
                        source,
                        start,
                        length,
                        output,
                        s["style"]
                    )

                    if output.stat().st_size > MAX_TG_UPLOAD_MB * 1024 * 1024:
                        continue

                    with output.open("rb") as file:
                        await bot.send_video(
                            chat_id,
                            video=file,
                            caption=f"✂️ VIRALX SHORT {index}",
                            supports_streaming=True,
                            read_timeout=300,
                            write_timeout=300
                        )

                    completed += 1

                await status.edit_text(
                    f"✅ Completed! {completed} Shorts created."
                )

    except Exception as error:
        log.exception("Processing failed")

        try:
            await status.edit_text(
                "❌ " + str(error)[:900]
            )
        except TelegramError:
            pass

    finally:
        shutil.rmtree(folder, ignore_errors=True)

        try:
            await bot.send_message(
                chat_id,
                "What would you like to do next?",
                reply_markup=main_menu()
            )
        except TelegramError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg.initialize()
    await tg.start()

    if PUBLIC_URL:
        await tg.bot.set_webhook(
            url=f"{PUBLIC_URL}/telegram",
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True
        )

    yield

    if PUBLIC_URL:
        await tg.bot.delete_webhook()

    await tg.stop()
    await tg.shutdown()


tg = ApplicationBuilder().token(TOKEN).updater(None).build()

tg.add_handler(CommandHandler("start", start))
tg.add_handler(CommandHandler("cancel", cancel))
tg.add_handler(CallbackQueryHandler(buttons))
tg.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text
    )
)
tg.add_handler(
    MessageHandler(
        filters.VIDEO | filters.Document.ALL,
        handle_video
    )
)

app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {
        "status": "online",
        "service": "VIRALX AI STUDIO",
        "version": "1.2"
    }


@app.post("/telegram")
async def webhook(request: Request):
    if WEBHOOK_SECRET:
        supplied = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if supplied != WEBHOOK_SECRET:
            raise HTTPException(status_code=403)

    payload = await request.json()
    update = Update.de_json(payload, tg.bot)

    await tg.process_update(update)

    return {"ok": True}
