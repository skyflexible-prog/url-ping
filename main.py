import os
import json
import time
import threading
import logging
from pathlib import Path
from typing import Dict, Any, List, Tuple

import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ---------- Config ----------
DATA_FILE = Path("config.json")
DEFAULT_INTERVAL = 300  # 5 minutes
MIN_INTERVAL = 30

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID")  # optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

GOOD_VALUES = {"ok", "healthy", "up"}

CONFIG_LOCK = threading.Lock()
URL_STATES: Dict[str, Dict[str, Any]] = {}


# ---------- Config helpers ----------
def load_config() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        return {"urls": [], "interval": DEFAULT_INTERVAL}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"urls": [], "interval": DEFAULT_INTERVAL}
        data.setdefault("urls", [])
        data.setdefault("interval", DEFAULT_INTERVAL)
        return data
    except Exception as e:
        logger.error("Error loading config: %s", e)
        return {"urls": [], "interval": DEFAULT_INTERVAL}


def save_config(cfg: Dict[str, Any]) -> None:
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.error("Error saving config: %s", e)


def get_urls_and_interval() -> Tuple[List[str], int]:
    with CONFIG_LOCK:
        cfg = load_config()
    return list(cfg["urls"]), int(cfg["interval"])


def update_config(urls=None, interval=None) -> Dict[str, Any]:
    with CONFIG_LOCK:
        cfg = load_config()
        if urls is not None:
            cfg["urls"] = urls
        if interval is not None:
            cfg["interval"] = int(interval)
        save_config(cfg)
    return cfg


# ---------- Health check ----------
def check_health(url: str) -> bool:
    try:
        resp = requests.get(url, timeout=10)
    except Exception as e:
        logger.warning("Request error for %s: %s", url, e)
        return False

    if not (200 <= resp.status_code < 400):
        logger.warning("Bad status %s for %s", resp.status_code, url)
        return False

    text = (resp.text or "").strip()

    if text and text.lower() in GOOD_VALUES:
        return True

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            val = str(data.get("status", "")).lower()
            if val in GOOD_VALUES:
                return True
    except Exception:
        pass

    return True


# ---------- Telegram notify ----------
async def notify_status_change(application, url: str, is_up: bool):
    if not ALLOWED_CHAT_ID:
        return
    text = f"✅ UP: {url}" if is_up else f"❌ DOWN: {url}"
    try:
        await application.bot.send_message(chat_id=int(ALLOWED_CHAT_ID), text=text)
    except Exception as e:
        logger.error("Failed to send Telegram message: %s", e)


# ---------- Ping loop ----------
async def async_ping_cycle(application):
    urls, interval = get_urls_and_interval()
    if urls:
        logger.info("Pinging %d URLs (interval=%ds)", len(urls), interval)
    else:
        logger.info("No URLs configured (interval=%ds)", interval)

    for url in urls:
        URL_STATES.setdefault(url, {"is_up": None, "fail_count": 0})

    for url in urls:
        is_up = check_health(url)
        state = URL_STATES.setdefault(url, {"is_up": None, "fail_count": 0})
        prev = state["is_up"]

        if is_up:
            if prev is False:
                state["is_up"] = True
                state["fail_count"] = 0
                await notify_status_change(application, url, True)
            else:
                state["is_up"] = True
                state["fail_count"] = 0
        else:
            state["fail_count"] += 1
            if state["fail_count"] >= 3 and prev in (True, None):
                state["is_up"] = False
                await notify_status_change(application, url, False)

    for u in list(URL_STATES.keys()):
        if u not in urls:
            del URL_STATES[u]


def ping_loop(application):
    while True:
        try:
            application.run_async(async_ping_cycle(application))
        except Exception as e:
            logger.error("Error in async_ping_cycle: %s", e)
        _, interval = get_urls_and_interval()
        time.sleep(max(MIN_INTERVAL, interval))


# ---------- Telegram handlers ----------
def is_authorized(update: Update) -> bool:
    if ALLOWED_CHAT_ID is None:
        return True
    return str(update.effective_chat.id) == str(ALLOWED_CHAT_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    _, interval = get_urls_and_interval()
    msg = (
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
    await update.message.reply_text(msg)


async def add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /add https://your-app.onrender.com/health")
        return
    url = context.args[0].strip()
    urls, _ = get_urls_and_interval()
    if url in urls:
        await update.message.reply_text("URL already in list.")
        return
    urls.append(url)
    update_config(urls=urls)
    URL_STATES.setdefault(url, {"is_up": None, "fail_count": 0})
    await update.message.reply_text(f"Added: {url}")


async def remove_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /remove https://your-app.onrender.com/health")
        return
    url = context.args[0].strip()
    urls, _ = get_urls_and_interval()
    if url not in urls:
        await update.message.reply_text("URL not found in list.")
        return
    urls.remove(url)
    update_config(urls=urls)
    URL_STATES.pop(url, None)
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
    lines = []
    for u in urls:
        state = URL_STATES.get(u, {"is_up": None})
        status = state["is_up"]
        if status is True:
            s = "UP"
        elif status is False:
            s = "DOWN"
        else:
            s = "UNKNOWN"
        lines.append(f"- {u} [{s}]")
    text = "Current URLs:
" + "
".join(lines)
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
        if seconds < MIN_INTERVAL:
            await update.message.reply_text(f"Minimum interval is {MIN_INTERVAL} seconds.")
            return
    except ValueError:
        await update.message.reply_text("Interval must be an integer.")
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

    if not DATA_FILE.exists():
        save_config({"urls": [], "interval": DEFAULT_INTERVAL})

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_url))
    application.add_handler(CommandHandler("remove", remove_url))
    application.add_handler(CommandHandler("list", list_urls))
    application.add_handler(CommandHandler("set_interval", set_interval))
    application.add_handler(CommandHandler("get_interval", get_interval))

    t = threading.Thread(target=ping_loop, args=(application,), daemon=True)
    t.start()

    logger.info("Bot starting...")
    application.run_polling()


if __name__ == "__main__":
    main()
