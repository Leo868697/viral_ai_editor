"""
VIRALX AI STUDIO
Version 1.0 - Phase 1
Telegram Downloader + Video to Shorts
FastAPI + Telegram Webhook + yt-dlp + FFmpeg
"""

import os
import re
import time
import asyncio
import logging
import shutil
import tempfile
import subprocess

from pathlib import Path
from contextlib import asynccontextmanager

import yt_dlp
import imageio_ffmpeg

from fastapi import FastAPI, Request, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =====================================================
# CONFIGURATION
# =====================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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
    os.getenv("PUBLIC_URL")
    or os.getenv("RENDER_EXTERNAL_URL", "")
).rstrip("/")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

OUT_W = 720
OUT_H = 1280

MAX_TG_DOWNLOAD = 20 * 1024 * 1024
MAX_TG_UPLOAD = 49 * 1024 * 1024
MAX_SOURCE_SIZE = 500 * 1024 * 1024
MAX_DURATION = 10 * 60 * 60

URL_PATTERN = re.compile(r"https?://[^\s]+")
START_TIME = time.time()

JOB_LOCK = asyncio.Semaphore(1)

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
# DOWNLOADER
# =====================================================

def download_video(url, folder):
    if not url.startswith(("http://", "https://")):
        raise ValueError("Only HTTP and HTTPS links are accepted.")

    output = str(Path(folder) / "%(id)s.%(ext)s")

    options = {
        "outtmpl": output,
        "format": "best[height<=1080]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG,
        "retries": 2,
        "socket_timeout": 30,
        "max_filesize": MAX_SOURCE_SIZE,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

    path = Path(filename)

    requested = info.get("requested_downloads") or []
    if requested and requested[0].get("filepath"):
        path = Path(requested[0]["filepath"])

    if not path.exists():
        candidates = list(Path(folder).glob(f"{info['id']}.*"))
        candidates = [
            p for p in candidates
            if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")
        ]
        if not candidates:
            raise FileNotFoundError("Downloaded video file not found.")
        path = candidates[0]

    if path.stat().st_size > MAX_SOURCE_SIZE:
        raise ValueError("Video exceeds the configured size limit.")

    return str(path), info.get("title") or "Video"


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
        score = (
            prefix[start + length] - prefix[start]
        ) / max(length, 1)

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

    if "Sign in to confirm" in message or "429" in message:
        return (
            "YouTube refused this server request. "
            "Try an authorized video upload or another supported source. "
            "Cookies cannot guarantee access."
        )

    if "Unsupported URL" in message:
        return "This link is not supported by the downloader."

    if "Private video" in message or "login" in message.lower():
        return "This video is private or requires authorization."

    if "too large" in message.lower():
        return "This video exceeds the configured server size limit."

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
        except BadRequest:
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

                source, title = await asyncio.to_thread(
                    download_video,
                    url,
                    temp_folder,
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
                size = os.path.getsize(source)

                if size > MAX_TG_UPLOAD:
                    await update_status(
                        "⚠️ Video exceeds Telegram's upload limit."
                    )
                else:
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

    await bot.send_message(
        chat_id,
        "What would you like to do next?",
        reply_markup=main_menu(),
    )


# =====================================================
# TELEGRAM COMMANDS
# =====================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = None

    await update.effective_message.reply_text(
        "🚀 WELCOME TO VIRALX AI STUDIO\n\n"
        "Your video downloader and Shorts creation workspace.\n\n"
        "Choose a service below:",
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
    await query.answer()

    data = query.data
    user_data = context.user_data
    s = get_settings(user_data)

    async def edit(message, keyboard=None):
        try:
            await query.message.edit_text(
                message,
                reply_markup=keyboard,
            )
        except BadRequest:
            pass

    if data == "menu":
        user_data["mode"] = None
        await edit(
            "🚀 VIRALX AI STUDIO\n\nChoose your service:",
            main_menu(),
        )

    elif data.startswith("mode:"):
        mode = data.split(":")[1]

        user_data["mode"] = (
            "shorts" if mode == "live" else mode
        )

        if mode == "download":
            message = (
                "📥 VIDEO DOWNLOADER\n\n"
                "Send a supported video link."
            )
        elif mode == "live":
            message = (
                "📡 RECORDING TO SHORTS\n\n"
                "Send an accessible recording link or upload a recording.\n"
                "This processes recordings, not an active live stream."
            )
        else:
            message = (
                "✂️ VIDEO TO SHORTS\n\n"
                "Send a video link or upload a video.\n\n"
                f"Current settings: {s['count']} clips × "
                f"{s['length']} seconds."
            )

        await edit(message, back_menu())

    elif data == "settings":
        await edit(
            "🎛 EDITOR SETTINGS\n\n"
            "Choose clip duration, clip count and framing:",
            settings_menu(s),
        )

    elif data.startswith("len:"):
        s["length"] = int(data.split(":")[1])
        await edit("✅ Clip duration updated.", settings_menu(s))

    elif data.startswith("count:"):
        s["count"] = int(data.split(":")[1])
        await edit("✅ Clip count updated.", settings_menu(s))

    elif data.startswith("style:"):
        s["style"] = data.split(":")[1]
        await edit("✅ Framing style updated.", settings_menu(s))

    elif data == "autopilot":
        await edit(
            "🤖 AUTOPILOT\n\n"
            "Automatic publishing is not connected yet.\n"
            "Official platform API connections are required.",
            back_menu(),
        )

    elif data == "publish":
        await edit(
            "📤 PUBLISHING CENTER\n\n"
            "YouTube, TikTok and Facebook publishing "
            "require official platform API connections.",
            back_menu(),
        )

    elif data == "status":
        uptime = int(time.time() - START_TIME)

        await edit(
            "📊 VIRALX SYSTEM STATUS\n\n"
            "System: Online\n"
            f"Uptime: {uptime // 3600}h "
            f"{(uptime % 3600) // 60}m\n"
            f"Resolution: {OUT_W}x{OUT_H}\n"
            f"Clip length: {s['length']} seconds\n"
            f"Clip count: {s['count']}\n"
            f"Frame style: {s['style']}\n"
            "Video engine: FFmpeg\n"
            "Downloader: yt-dlp",
            back_menu(),
        )


# =====================================================
# URL AND VIDEO HANDLERS
# =====================================================

async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
