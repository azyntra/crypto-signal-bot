"""
telegram_bot.py — Async Telegram bot for delivering signals and admin commands.
Uses python-telegram-bot v21 (async).
"""
import asyncio
from typing import Optional

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, TELEGRAM_ADMIN_ID
from config.logger import get_logger

logger = get_logger(__name__)

_bot: Optional[Bot] = None
_app: Optional[Application] = None

# Rate limiting: track messages sent per hour
_sent_this_hour: list = []
from config.settings import MAX_SIGNALS_PER_HOUR


async def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot


async def send_signal(text: str) -> Optional[int]:
    """
    Send a signal message to the configured Telegram channel.
    Returns the message_id on success, None on failure.
    Enforces hourly rate limit.
    """
    import time
    now = time.time()

    # Clean up messages older than 1 hour
    _sent_this_hour[:] = [t for t in _sent_this_hour if now - t < 3600]

    if len(_sent_this_hour) >= MAX_SIGNALS_PER_HOUR:
        logger.warning(f"Rate limit reached: {MAX_SIGNALS_PER_HOUR} signals/hour. Skipping.")
        return None

    try:
        bot = await get_bot()
        msg = await bot.send_message(
            chat_id    = TELEGRAM_CHANNEL_ID,
            text       = text,
            parse_mode = ParseMode.HTML,
            disable_web_page_preview = True,
        )
        _sent_this_hour.append(now)
        logger.info(f"Signal sent to Telegram (msg_id={msg.message_id})")
        return msg.message_id
    except TelegramError as e:
        logger.error(f"Telegram send error: {e}")
        return None


async def send_admin(text: str):
    """Send a message directly to the admin user."""
    if not TELEGRAM_ADMIN_ID:
        return
    try:
        bot = await get_bot()
        await bot.send_message(
            chat_id    = TELEGRAM_ADMIN_ID,
            text       = text,
            parse_mode = ParseMode.HTML,
            disable_web_page_preview = True,
        )
    except TelegramError as e:
        logger.warning(f"Admin message failed: {e}")


async def send_startup_message(text: str):
    """Send to both channel and admin."""
    await send_signal(text)
    await send_admin(text)


# ── Bot command handlers ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Crypto Signal Bot</b> is running.\n"
        "Use /stats to see signal performance.\n"
        "Use /status to check scanner health.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.database.db_logger import get_stats
    s = get_stats(days=7)
    text = (
        f"📊 <b>Signal Stats (last 7 days)</b>\n\n"
        f"Total signals: {s['total']}\n"
        f"Wins (TP hit):  {s['wins']}\n"
        f"Losses (SL hit): {s['losses']}\n"
        f"Open: {s['open']}\n"
        f"Win rate: <b>{s['win_rate']}%</b>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    signals_this_hour = len(_sent_this_hour)
    await update.message.reply_text(
        f"✅ Bot is alive\n"
        f"Signals sent this hour: {signals_this_hour}/{MAX_SIGNALS_PER_HOUR}",
        parse_mode=ParseMode.HTML,
    )


def build_application() -> Application:
    """Build the Telegram Application with command handlers registered."""
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("status", cmd_status))
    return app
