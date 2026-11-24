import os
import re
import asyncio
import tempfile
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import yt_dlp
import imageio_ffmpeg

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "")
MAX_BYTES = 49 * 1024 * 1024

tg = Application.builder().token(TOKEN).updater(None).build()


def download(url, folder):
    opts = {
        "outtmpl": f"{folder}/%(id)s.%(ext)s",
        "format": "best[ext=mp4][filesize<48M]/best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "max_filesize": MAX_BYTES,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


async def start(update, context):
    await update.message.reply_text("ViralX AI Editor ready. Video ka link bhejo.")


async def handle_link(update, context):
    m = re.search(r"https?://\S+", update.message.text or "")
    if not m:
        await update.message.reply_text("Koi video link bhejo (https://...).")
        return
    await update.message.reply_text("Download ho raha hai, thora ruko...")
    with tempfile.TemporaryDirectory() as folder:
        try:
            path = await asyncio.to_thread(download, m.group(0), folder)
            if os.path.getsize(path) > MAX_BYTES:
                await update.message.reply_text("Video 50MB se bara hai, Telegram nahi bhejne dega.")
                return
            with open(path, "rb") as f:
                await update.message.reply_video(
                    video=f, read_timeout=120, write_timeout=120
                )
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)[:300]}")


tg.add_handler(CommandHandler("start", start))
tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))


@asynccontextmanager
async def lifespan(app):
    await tg.initialize()
    await tg.start()
    if BASE_URL:
        await tg.bot.set_webhook(f"{BASE_URL}/webhook")
    yield
    await tg.stop()
    await tg.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def home():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    await tg.process_update(Update.de_json(data, tg.bot))
    return {"ok": True}
