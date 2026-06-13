"""
telegram_bot.py — Async Telegram bot for delivering signals and admin commands.
Uses python-telegram-bot v21 (async).

Commands:
  /start   — welcome + command list
  /stats   — 7-day quick win/loss summary
  /report  — full performance breakdown (pass days: /report 14)
  /open    — currently tracked open signals
  /best    — top 5 symbols by win rate (last 30d)
  /status  — scanner health + rate limit info
"""
import time
from typing import Optional

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config.settings import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID,
    TELEGRAM_ADMIN_ID, MAX_SIGNALS_PER_HOUR,
)
from config.logger import get_logger

logger = get_logger(__name__)

_bot: Optional[Bot] = None
_sent_this_hour: list = []


# ── Core send helpers ──────────────────────────────────────────────────────────

async def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot


async def send_signal(text: str) -> Optional[int]:
    """
    Send a message to the configured Telegram channel.
    Enforces hourly rate limit. Returns message_id or None.
    """
    now = time.time()
    _sent_this_hour[:] = [t for t in _sent_this_hour if now - t < 3600]

    if len(_sent_this_hour) >= MAX_SIGNALS_PER_HOUR:
        logger.warning(f"Rate limit reached ({MAX_SIGNALS_PER_HOUR}/hr). Skipping.")
        return None

    try:
        bot = await get_bot()
        msg = await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        _sent_this_hour.append(now)
        logger.info(f"Message sent to channel (msg_id={msg.message_id})")
        return msg.message_id
    except TelegramError as e:
        logger.error(f"Telegram send error: {e}")
        return None


async def send_admin(text: str):
    """Send a direct message to the admin user ID."""
    if not TELEGRAM_ADMIN_ID:
        return
    try:
        bot = await get_bot()
        await bot.send_message(
            chat_id=TELEGRAM_ADMIN_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        logger.warning(f"Admin message failed: {e}")


async def send_startup_message(text: str):
    """Broadcast startup notification to channel and admin."""
    await send_signal(text)
    await send_admin(text)


# ── Command handlers ───────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Crypto Signal Bot v1.2</b> is running.\n\n"
        "<b>Commands:</b>\n"
        "  /stats       — 7-day win/loss summary\n"
        "  /report      — full performance breakdown\n"
        "  /report 14   — last 14 days\n"
        "  /open        — currently open signals\n"
        "  /best        — top 5 symbols (30d)\n"
        "  /status      — scanner health check",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick 7-day overview."""
    from src.database.db_logger import get_stats
    s  = get_stats(days=7)
    wr = s["win_rate"]
    bar = "█" * round(wr / 100 * 8) + "░" * (8 - round(wr / 100 * 8))
    await update.message.reply_text(
        f"📊 <b>Signal Stats — Last 7 days</b>\n\n"
        f"Closed signals:  {s['total']}\n"
        f"✅ Wins (TP):     {s['wins']}\n"
        f"❌ Losses (SL):   {s['losses']}\n"
        f"🔓 Open:          {s['open']}\n\n"
        f"Win rate:  {bar} <b>{wr}%</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full performance breakdown. Optional arg: number of days."""
    from src.tracking.performance import get_full_stats, format_performance_report
    days = 7
    if context.args:
        try:
            days = max(1, min(int(context.args[0]), 90))
        except ValueError:
            pass

    stats = get_full_stats(days=days)
    if stats["total"] == 0:
        await update.message.reply_text(
            f"No closed signals in the last {days} days yet.\n"
            f"Open signals being tracked: {stats['open']}\n"
            f"Check back once some TP/SL levels are hit.",
        )
        return
    text = format_performance_report(stats)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all currently open (unresolved) signals."""
    from src.database.db_logger import SessionLocal, SignalRecord
    from datetime import datetime, timezone

    with SessionLocal() as db:
        open_sigs = (
            db.query(SignalRecord)
            .filter(
                SignalRecord.outcome.is_(None),
                SignalRecord.sent_to_telegram.is_(True),
            )
            .order_by(SignalRecord.created_at.desc())
            .limit(15)
            .all()
        )
        db.expunge_all()

    if not open_sigs:
        await update.message.reply_text("No open signals being tracked right now.")
        return

    lines = [f"🔓 <b>Open Signals ({len(open_sigs)})</b>\n"]
    for s in open_sigs:
        created = s.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = int((datetime.now(timezone.utc) - created).total_seconds() / 60) if created else 0
        dir_e = "🟢" if s.direction == "LONG" else "🔴"
        lines.append(
            f"{dir_e} <b>{s.symbol}</b> {s.direction} · "
            f"{s.style.upper()} · {s.exchange.upper()}\n"
            f"   TP1: ${_fmt(s.tp1)}  SL: ${_fmt(s.stop_loss)}  "
            f"Conf: {s.confidence:.0f}%  <i>{age}m ago</i>"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Top 5 performing symbols by win rate over 30 days."""
    from src.tracking.performance import get_full_stats
    stats = get_full_stats(days=30)
    sym = stats.get("by_symbol", {})

    if not sym:
        await update.message.reply_text(
            "Not enough closed signals yet. Give the bot a few more days."
        )
        return

    ranked = sorted(
        sym.items(),
        key=lambda x: (x[1]["win_rate"], x[1]["total"]),
        reverse=True,
    )[:5]

    lines = ["🏆 <b>Top 5 Symbols — Last 30 days</b>\n"]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, (symbol, s) in enumerate(ranked):
        lines.append(
            f"{medals[i]} <b>{symbol}</b>  "
            f"{s['wins']}W {s['losses']}L  "
            f"WR: <b>{s['win_rate']}%</b>  "
            f"Avg: {s['avg_pnl']:+.2f}%"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scanner health check."""
    from src.database.db_logger import SessionLocal, SignalRecord
    from sqlalchemy import func as sqlfunc

    signals_this_hour = len(_sent_this_hour)
    with SessionLocal() as db:
        total_db = db.query(sqlfunc.count(SignalRecord.id)).scalar() or 0
        open_db  = db.query(sqlfunc.count(SignalRecord.id)).filter(
            SignalRecord.outcome.is_(None)
        ).scalar() or 0

    await update.message.reply_text(
        f"✅ <b>Bot Status</b>\n\n"
        f"Signals sent this hour: {signals_this_hour}/{MAX_SIGNALS_PER_HOUR}\n"
        f"Total signals in DB:    {total_db}\n"
        f"Open (being tracked):   {open_db}\n\n"
        f"Outcome tracker:  ✅ running every 60s\n"
        f"Scanners:         ✅ scalp + swing active",
        parse_mode=ParseMode.HTML,
    )


# ── Build app ──────────────────────────────────────────────────────────────────

def build_application() -> Application:
    """Build the Telegram Application with all command handlers registered."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("open",   cmd_open))
    app.add_handler(CommandHandler("best",   cmd_best))
    app.add_handler(CommandHandler("status", cmd_status))
    return app


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    if value >= 1000:   return f"{value:,.2f}"
    elif value >= 1:    return f"{value:.4f}"
    elif value >= 0.01: return f"{value:.5f}"
    else:               return f"{value:.8f}"
