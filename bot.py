import os
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("9128031239:AAHJkrgFlNhY-PpobFB75FiyIeGgXnWeETQ")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

telegram_app = Application.builder().token(BOT_TOKEN).updater(None).build()
app = FastAPI()

ALLOWED_SITES = [
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
]

def valid_video_url(url):
    try:
        host = urlparse(url).hostname or ""
        return any(
            host == site or host.endswith("." + site)
            for site in ALLOWED_SITES
        )
    except Exception:
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to ViralX AI Editor! 🎬\n\n"
        "Send me a public video link to get started.\n\n"
        "Commands:\n"
        "/start - Start bot\n"
        "/help - Help\n"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send a supported public video link:\n"
        "YouTube, Instagram, TikTok or Facebook.\n\n"
        "AI editing engine is being set up."
    )


async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    url = (message.text or "").strip()

    if not valid_video_url(url):
        await message.reply_text(
            "Please send a valid public video link from a supported platform."
        )
        return

    await message.reply_text(
        "✅ Video link received!\n\n"
        "Your editing engine is under construction. "
        "Automatic editing will be enabled in the next stage."
    )


telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("help", help_command))
telegram_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)
)


@app.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()

    if WEBHOOK_URL:
        await telegram_app.bot.set_webhook(
            url=f"{WEBHOOK_URL.rstrip('/')}/telegram"
        )


@app.on_event("shutdown")
async def shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


@app.post("/telegram")
async def telegram_webhook(request: Request):
    if not WEBHOOK_URL:
        raise HTTPException(status_code=503)

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


@app.get("/")
async def home():
    return {"status": "ViralX AI Editor is running"}
  
