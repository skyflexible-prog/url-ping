import os
import json
import time
import threading
import logging
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ---------- Config & persistence ----------
DATA_FILE = Path("config.json")
DEFAULT_INTERVAL = 300  # 5 minutes

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")  # str


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def load_config():
    if not DATA_FILE.exists():
        return {"urls": [], "interval": DEFAULT_INTERVAL}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if "urls" not in data or "interval" not in data:
            return {"urls": data if isinstance(data, list) else [], "interval": DEFAULT_INTERVAL}
        return data
    except Exception as e:
        logger.error("Error loading config: %s", e)
        return {"urls": [], "interval": DEFAULT_INTERVAL}


def save_config(cfg):
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.error("Error saving config: %s", e)


# keep config in memory, but re-read each cycle to be safe for Railway restarts
CONFIG_LOCK = threading.Lock()


def get_urls_and_interval():
    with CONFIG_LOCK:
        cfg = load_config()
    return cfg["urls"], cfg["interval"]


def update_config(urls=None, interval=None):
    with CONFIG_LOCK:
        cfg = load_config()
        if urls is not None:
            cfg["urls"] = urls
        if interval is not None:
            cfg["interval"] = interval
        save_config(cfg)
    return cfg


# ---------- Pinger thread ----------
def ping_loop():
    while True:
        urls, interval = get_urls_and_interval()
        if urls:
            logger.info("Pinging %d URLs (interval=%ds)", len(urls), interval)
        else:
            logger.info("No URLs configured, sleeping (interval=%ds)", interval)

        for url in urls:
            try:
                resp = requests.get(url, timeout=10)
                logger.info("Ping %s -> %s", url, resp.status_code)
            except Exception as e:
                logger.warning("Ping failed for %s: %s", url, e)

        # sleep based on current interval (reloaded every loop)
        time.sleep(max(30, interval))  # hard minimum of 30s to avoid abuse


# ---------- Telegram bot helpers ----------
def is_authorized(update: Update) -> bool:
    if ALLOWED_CHAT_ID is None:
        return True
    chat_id = update.effective_chat.id
    return str(chat_id) == str(ALLOWED_CHAT_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    _, interval = get_urls_and_interval()
    await update.message.reply_text(
        "Uptime bot online.
"
        "Commands:
"
        "/add <url>
"
        "/remove <url>
"
        "/list
"
        "/set_interval <seconds>
"
        "/get_interval

"
        f"Current interval: {interval} seconds"
    )


async def add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /add https://your-app.onrender.com")
        return

    url = context.args[0].strip()
    urls, _ = get_urls_and_interval()
    if url in urls:
        await update.message.reply_text("URL already in list.")
        return

    urls.append(url)
    update_config(urls=urls)
    await update.message.reply_text(f"Added: {url}")


async def remove_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /remove https://your-app.onrender.com")
        return

    url = context.args[0].strip()
    urls, _ = get_urls_and_interval()
    if url not in urls:
        await update.message.reply_text("URL not found in list.")
        return

    urls.remove(url)
    update_config(urls=urls)
    await update.message.reply_text(f"Removed: {url}")


async def list_urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    urls, interval = get_urls_and_interval()
    if not urls:
        await update.message.reply_text(
            f"No URLs configured.
Current interval: {interval} seconds"
        )
        return
    text = "Current URLs:
" + "
".join(f"- {u}" for u in urls)
    text += f"

Current interval: {interval} seconds"
    await update.message.reply_text(text)


async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /set_interval <seconds>")
        return

    try:
        seconds = int(context.args[0])
        if seconds < 30:
            await update.message.reply_text("Minimum interval is 30 seconds.")
            return
    except ValueError:
        await update.message.reply_text("Interval must be an integer number of seconds.")
        return

    cfg = update_config(interval=seconds)
    await update.message.reply_text(
        f"Ping interval updated to {cfg['interval']} seconds."
    )


async def get_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    _, interval = get_urls_and_interval()
    await update.message.reply_text(f"Current ping interval: {interval} seconds.")


# ---------- Main ----------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    # Ensure config file exists
    if not DATA_FILE.exists():
        save_config({"urls": [], "interval": DEFAULT_INTERVAL})

    # Start background pinger thread
    t = threading.Thread(target=ping_loop, daemon=True)
    t.start()

    # Telegram bot
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_url))
    application.add_handler(CommandHandler("remove", remove_url))
    application.add_handler(CommandHandler("list", list_urls))
    application.add_handler(CommandHandler("set_interval", set_interval))
    application.add_handler(CommandHandler("get_interval", get_interval))

    logger.info("Bot starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
