"""
telegram_bot.py — Async Telegram delivery + commands (v3).

Commands:
  /start     — command list
  /stats     — quick 7-day summary (honest: R-based)
  /report    — full performance breakdown (/report 14)
  /open      — open + pending signals
  /best      — top symbols by realized R (30d)
  /equity    — equity curve chart
  /backtest  — /backtest BTC [intraday|swing] [days]
  /ai        — /ai SOL — on-demand AI analysis of a coin
  /regime    — current BTC + market regime
  /events    — upcoming high-impact events
  /addevent  — (admin) /addevent 2026-07-15 18:00 | FOMC
  /status    — scanner health
"""
import io
from typing import Optional

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config.settings import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, TELEGRAM_ADMIN_ID,
    QUOTE_CURRENCY, TRACK_EXCHANGE, MARKET_TYPE, VERSION,
)
from config.logger import get_logger

logger = get_logger(__name__)

_bot: Optional[Bot] = None


async def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot


# ── Send helpers ──────────────────────────────────────────────────────────────

async def send_signal(text: str) -> Optional[int]:
    try:
        bot = await get_bot()
        msg = await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text,
                                     parse_mode=ParseMode.HTML,
                                     disable_web_page_preview=True)
        return msg.message_id
    except TelegramError as e:
        logger.error(f"Telegram send error: {e}")
        return None


async def send_signal_with_chart(text: str, chart_png: bytes) -> Optional[int]:
    """Send chart photo with the signal as caption (falls back to text)."""
    try:
        bot = await get_bot()
        if len(text) <= 1024:
            msg = await bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID,
                                       photo=io.BytesIO(chart_png),
                                       caption=text, parse_mode=ParseMode.HTML)
            return msg.message_id
        msg = await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text,
                                     parse_mode=ParseMode.HTML,
                                     disable_web_page_preview=True)
        await bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=io.BytesIO(chart_png),
                             reply_to_message_id=msg.message_id)
        return msg.message_id
    except TelegramError as e:
        logger.error(f"Telegram chart send error: {e}")
        return await send_signal(text)


async def send_result(text: str) -> Optional[int]:
    return await send_signal(text)


async def send_admin(text: str):
    if not TELEGRAM_ADMIN_ID:
        return
    try:
        bot = await get_bot()
        await bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=text,
                               parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True)
    except TelegramError as e:
        logger.warning(f"Admin message failed: {e}")


async def send_startup_message(text: str):
    await send_signal(text)
    await send_admin(text)


def _is_admin(update: Update) -> bool:
    return TELEGRAM_ADMIN_ID and str(update.effective_user.id) == str(TELEGRAM_ADMIN_ID)


def _to_pair(arg: str) -> str:
    sym = arg.upper().replace("/", "").replace("USDT", "")
    return f"{sym}/{QUOTE_CURRENCY}"


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 <b>Crypto Signal Bot v{VERSION}</b>\n\n"
        "<b>Commands:</b>\n"
        "  /stats — 7-day summary\n"
        "  /report [days] — full breakdown\n"
        "  /open — open signals\n"
        "  /best — top symbols (30d)\n"
        "  /equity — equity curve\n"
        "  /backtest BTC [intraday|swing] [days]\n"
        "  /ai SOL — AI analysis of a coin\n"
        "  /regime — market regime now\n"
        "  /events — upcoming event guard windows\n"
        "  /status — scanner health",
        parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.database.db_logger import get_stats
    s = get_stats(days=7)
    wr = s["win_rate"]
    bar = "█" * round(wr / 100 * 8) + "░" * (8 - round(wr / 100 * 8))
    await update.message.reply_text(
        f"📊 <b>Last 7 days</b>\n\n"
        f"Closed: {s['total']}  (✅{s['wins']} / ❌{s['losses']} / ⚪{s['breakeven']})\n"
        f"Open: {s['open']}   Unfilled: {s['nofill']}\n\n"
        f"Win rate:  {bar} <b>{wr}%</b>\n"
        f"Net: <b>{s['total_r']:+.2f}R</b>  ·  Expectancy: <b>{s['expectancy_r']:+.3f}R</b>",
        parse_mode=ParseMode.HTML)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            f"No closed trades in the last {days} days. Open: {stats['open']}")
        return
    await update.message.reply_text(format_performance_report(stats),
                                    parse_mode=ParseMode.HTML)


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.database.db_logger import SessionLocal, SignalRecord
    from datetime import datetime, timezone
    with SessionLocal() as db:
        sigs = (db.query(SignalRecord)
                .filter(SignalRecord.outcome.is_(None),
                        SignalRecord.sent_to_telegram.is_(True))
                .order_by(SignalRecord.created_at.desc()).limit(15).all())
        db.expunge_all()
    if not sigs:
        await update.message.reply_text("No open signals right now.")
        return
    lines = [f"🔓 <b>Open Signals ({len(sigs)})</b>\n"]
    for s in sigs:
        created = s.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = int((datetime.now(timezone.utc) - created).total_seconds() / 60) if created else 0
        status = "⏳ pending fill" if (s.status or "PENDING") == "PENDING" else "▶️ active"
        dir_e = "🟢" if s.direction == "LONG" else "🔴"
        lines.append(f"{dir_e} <b>{s.symbol}</b> {s.direction} · {s.style} · {status}\n"
                     f"   SL ${s.stop_loss}  TP1 ${s.tp1}  ·  {age}m ago")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.tracking.performance import get_full_stats
    stats = get_full_stats(days=30)
    sym = stats.get("by_symbol", {})
    if not sym:
        await update.message.reply_text("Not enough closed trades yet.")
        return
    ranked = sorted(sym.items(), key=lambda x: -x[1]["r"])[:5]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines = ["🏆 <b>Top symbols by realized R — 30d</b>\n"]
    for i, (symbol, s) in enumerate(ranked):
        lines.append(f"{medals[i]} <b>{symbol}</b>  {s['wins']}W/{s['losses']}L  "
                     f"WR {s['win_rate']}%  <b>{s['r']:+.1f}R</b>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_equity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.tracking.performance import get_full_stats
    from src.signals.charting import render_equity_curve
    days = 30
    if context.args:
        try:
            days = max(7, min(int(context.args[0]), 90))
        except ValueError:
            pass
    stats = get_full_stats(days=days)
    png = render_equity_curve(stats.get("closed_records", []))
    if not png:
        await update.message.reply_text("Not enough closed trades for an equity curve yet.")
        return
    await update.message.reply_photo(photo=io.BytesIO(png),
                                     caption=f"Equity curve — last {days} days "
                                             f"({stats['total_r']:+.2f}R)")


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.backtest.engine import run_backtest, format_backtest_report
    if not context.args:
        await update.message.reply_text("Usage: /backtest BTC [intraday|swing] [days]")
        return
    symbol = _to_pair(context.args[0])
    style = "intraday"
    days = 30
    for a in context.args[1:]:
        if a.lower() in ("intraday", "swing"):
            style = a.lower()
        else:
            try:
                days = max(7, min(int(a), 90))
            except ValueError:
                pass
    await update.message.reply_text(
        f"🧪 Backtesting {symbol} ({style}, {days}d)... this takes a minute or two.")
    try:
        result = await run_backtest(symbol, style, days)
        if result is None:
            await update.message.reply_text(f"Couldn't fetch history for {symbol}.")
            return
        await update.message.reply_text(format_backtest_report(result.stats()),
                                        parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Backtest command error: {e}")
        await update.message.reply_text(f"Backtest failed: {e}")


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.data.fetcher import fetch_multi_timeframe
    from src.analysis.indicators import compute_indicators
    from src.analysis.regime import classify_regime
    from src.analysis.ai_filter import analyze_symbol_ai
    if not context.args:
        await update.message.reply_text("Usage: /ai SOL")
        return
    symbol = _to_pair(context.args[0])
    await update.message.reply_text(f"🤖 Analyzing {symbol}...")
    data = await fetch_multi_timeframe(TRACK_EXCHANGE, symbol, ["1h", "4h"], MARKET_TYPE)
    ind_1h = compute_indicators(data.get("1h"))
    ind_4h = compute_indicators(data.get("4h"))
    if not ind_4h:
        await update.message.reply_text(f"No data for {symbol}.")
        return
    regime = classify_regime(ind_4h, ind_1h)
    text = await analyze_symbol_ai(symbol, ind_1h or {}, ind_4h, regime)
    await update.message.reply_text(
        f"🤖 <b>{symbol}</b> (regime: {regime})\n\n{text or 'AI unavailable.'}",
        parse_mode=ParseMode.HTML)


async def cmd_regime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.analysis.regime import get_btc_regime
    from src.analysis.sentiment import get_fear_greed_index
    btc = await get_btc_regime()
    fg = get_fear_greed_index()
    shock = "🔴 ACTIVE (signals paused)" if btc.get("shock") else "🟢 clear"
    await update.message.reply_text(
        f"🧭 <b>Market Regime</b>\n\n"
        f"BTC regime: <b>{btc.get('regime')}</b>\n"
        f"BTC 1h RSI: {btc.get('rsi_1h') and round(btc['rsi_1h'], 1)}\n"
        f"Shock breaker: {shock}\n"
        f"Fear & Greed: {fg.get('value') if fg else 'N/A'} ({fg.get('label') if fg else ''})",
        parse_mode=ParseMode.HTML)


async def cmd_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.analysis.regime import get_upcoming_events
    events = get_upcoming_events(hours=72)
    if not events:
        await update.message.reply_text(
            "No events in the next 72h.\nAdmins add them with:\n"
            "/addevent 2026-07-15 18:00 | FOMC rate decision")
        return
    lines = ["📅 <b>Upcoming events (signal guard)</b>\n"]
    for ev in events:
        lines.append(f" • {ev['time_utc']} UTC — {ev['name']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_addevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.analysis.regime import add_event
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return
    raw = " ".join(context.args or [])
    if "|" not in raw:
        await update.message.reply_text("Format: /addevent 2026-07-15 18:00 | FOMC")
        return
    when, name = [p.strip() for p in raw.split("|", 1)]
    if add_event(name, when):
        await update.message.reply_text(f"✅ Added: {when} UTC — {name}")
    else:
        await update.message.reply_text("Bad datetime. Use: YYYY-MM-DD HH:MM")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.database.db_logger import SessionLocal, SignalRecord
    from sqlalchemy import func as sqlfunc
    with SessionLocal() as db:
        total = db.query(sqlfunc.count(SignalRecord.id)).scalar() or 0
        open_ = db.query(sqlfunc.count(SignalRecord.id)).filter(
            SignalRecord.outcome.is_(None)).scalar() or 0
    await update.message.reply_text(
        f"✅ <b>Bot v{VERSION}</b>\n\n"
        f"Signals in DB: {total}\nOpen: {open_}\n\n"
        f"Intraday scanner: every 15m\nSwing scanner: every 60m\n"
        f"Outcome tracker: every 60s (1m-candle accurate)",
        parse_mode=ParseMode.HTML)


def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    for name, fn in [
        ("start", cmd_start), ("stats", cmd_stats), ("report", cmd_report),
        ("open", cmd_open), ("best", cmd_best), ("equity", cmd_equity),
        ("backtest", cmd_backtest), ("ai", cmd_ai), ("regime", cmd_regime),
        ("events", cmd_events), ("addevent", cmd_addevent), ("status", cmd_status),
    ]:
        app.add_handler(CommandHandler(name, fn))
    return app
