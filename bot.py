import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "")

tg = Application.builder().token(TOKEN).updater(None).build()

async def start(update, context):
    await update.message.reply_text("ViralX AI Editor ready. Link bhejo.")

tg.add_handler(CommandHandler("start", start))

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
