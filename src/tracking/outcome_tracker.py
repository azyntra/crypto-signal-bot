"""
outcome_tracker.py — Monitors all open signals and automatically records
whether TP1 / TP2 / TP3 / SL was hit.

Logic:
  - Every 60 seconds, fetch the latest price for every open signal
  - Check if price crossed any TP or SL level
  - Record outcome + profit % + close time in the DB
  - Send a result card to Telegram
  - Auto-expire signals that exceed their max validity window
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.database.db_logger import SessionLocal, SignalRecord
from src.data.fetcher import get_exchange
from src.delivery.telegram_bot import send_signal
from config.logger import get_logger

logger = get_logger(__name__)

# Maximum time before a signal is auto-expired if neither TP nor SL is hit
EXPIRY_HOURS = {
    "scalp": 2,    # scalp signals live for 2 hours max
    "swing": 72,   # swing signals live for 3 days max
}


# ── Price fetching ─────────────────────────────────────────────────────────────

async def _get_current_price(exchange_name: str, symbol: str, market_type: str) -> Optional[float]:
    """Fetch the latest traded price for a symbol via ccxt ticker."""
    try:
        ex = await get_exchange(exchange_name, market_type)
        ticker = await ex.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if price:
            return float(price)
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if bid and ask:
            return (float(bid) + float(ask)) / 2.0
    except Exception as e:
        logger.debug(f"Price fetch failed [{exchange_name}] {symbol}: {e}")
    return None


# ── Level checks ──────────────────────────────────────────────────────────────

def _check_levels(price: float, rec: SignalRecord) -> Optional[str]:
    """
    Return the best outcome level hit at the current price, or None.
    Priority: TP3 > TP2 > TP1 > SL (we reward the best level hit).
    """
    d = rec.direction
    if d == "LONG":
        if rec.tp3 and price >= rec.tp3:   return "TP3"
        if rec.tp2 and price >= rec.tp2:   return "TP2"
        if rec.tp1 and price >= rec.tp1:   return "TP1"
        if rec.stop_loss and price <= rec.stop_loss: return "SL"
    elif d == "SHORT":
        if rec.tp3 and price <= rec.tp3:   return "TP3"
        if rec.tp2 and price <= rec.tp2:   return "TP2"
        if rec.tp1 and price <= rec.tp1:   return "TP1"
        if rec.stop_loss and price >= rec.stop_loss: return "SL"
    return None


def _calc_profit(outcome: str, rec: SignalRecord) -> float:
    """Calculate actual profit/loss % based on which level was hit."""
    entry = rec.price_at_signal
    if not entry or entry == 0:
        return 0.0
    target_map = {"TP1": rec.tp1, "TP2": rec.tp2, "TP3": rec.tp3, "SL": rec.stop_loss}
    target = target_map.get(outcome)
    if not target:
        return 0.0
    if rec.direction == "LONG":
        return round((target - entry) / entry * 100, 3)
    else:
        return round((entry - target) / entry * 100, 3)


def _calc_expired_pnl(current_price: float, rec: SignalRecord) -> float:
    """P&L at expiry — just mark-to-market from entry."""
    entry = rec.price_at_signal
    if not entry or entry == 0:
        return 0.0
    if rec.direction == "LONG":
        return round((current_price - entry) / entry * 100, 3)
    else:
        return round((entry - current_price) / entry * 100, 3)


# ── DB writes ─────────────────────────────────────────────────────────────────

def _close_signal_in_db(signal_id: int, outcome: str, price: float, profit_pct: float):
    """Persist the outcome to the signals table."""
    with SessionLocal() as db:
        rec = db.get(SignalRecord, signal_id)
        if rec:
            rec.outcome        = outcome
            rec.price_at_close = price
            rec.profit_pct     = profit_pct
            rec.closed_at      = datetime.now(timezone.utc)
            db.commit()
    logger.info(f"Signal #{signal_id} → {outcome}  {profit_pct:+.2f}%")


def _get_open_signals() -> list[SignalRecord]:
    """Load all unresolved signals from the DB."""
    with SessionLocal() as db:
        recs = db.query(SignalRecord).filter(
            SignalRecord.outcome.is_(None),
            SignalRecord.sent_to_telegram.is_(True),
        ).all()
        db.expunge_all()
        return recs


# ── Telegram result card ───────────────────────────────────────────────────────

def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    if value >= 1000:
        return f"{value:,.2f}"
    elif value >= 1:
        return f"{value:.4f}"
    elif value >= 0.01:
        return f"{value:.5f}"
    else:
        return f"{value:.8f}"


def _elapsed(rec: SignalRecord) -> str:
    """Human-readable elapsed time since signal was created."""
    created = rec.created_at
    if not created:
        return "?"
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - created
    total_min = int(delta.total_seconds() / 60)
    if total_min < 60:
        return f"{total_min}m"
    h, m = divmod(total_min, 60)
    return f"{h}h {m}m"


def _build_result_message(rec: SignalRecord, outcome: str, exit_price: float, profit_pct: float) -> str:
    is_win  = outcome in ("TP1", "TP2", "TP3")
    is_expr = outcome == "EXPIRED"

    emoji_map = {
        "TP3": "🏆", "TP2": "✅", "TP1": "✅", "SL": "❌", "EXPIRED": "⏰"
    }
    label_map = {
        "TP3": "FULL TARGET HIT",
        "TP2": "TARGET 2 HIT",
        "TP1": "TARGET 1 HIT",
        "SL":  "STOPPED OUT",
        "EXPIRED": "SIGNAL EXPIRED",
    }

    result_emoji = emoji_map.get(outcome, "⚪")
    result_label = label_map.get(outcome, outcome)
    dir_emoji    = "🟢" if rec.direction == "LONG" else "🔴"
    style_emoji  = "⚡" if rec.style == "scalp" else "📈"
    sign         = "+" if profit_pct >= 0 else ""
    pnl_emoji    = "💚" if profit_pct >= 0 else "🔴"

    footer_msg = (
        "🟢 Profitable trade! Well done."      if is_win else
        "⏰ Expired without hitting TP or SL." if is_expr else
        "🔴 Stopped out. Risk managed."
    )

    return "\n".join([
        "─" * 32,
        f"{result_emoji} <b>SIGNAL RESULT — {outcome}</b>",
        "─" * 32,
        "",
        f"📊 <b>{rec.symbol}</b>  ·  {rec.exchange.upper()}  ·  {rec.market_type.upper()}",
        f"{dir_emoji} {rec.direction}  |  {style_emoji} {rec.style.upper()}  |  Conf: {rec.confidence:.0f}%",
        "",
        f"<b>{result_label}</b>  {result_emoji}",
        "",
        f"📍 Entry:     ${_fmt_price(rec.price_at_signal)}",
        f"📍 Exit:      ${_fmt_price(exit_price)}",
        f"{pnl_emoji} P&amp;L:      <b>{sign}{profit_pct:.2f}%</b>",
        f"⏱ Duration:  {_elapsed(rec)}",
        "",
        f"🎯 Targets were:  TP1 ${_fmt_price(rec.tp1)}  TP2 ${_fmt_price(rec.tp2)}  TP3 ${_fmt_price(rec.tp3)}",
        f"🛡 Stop Loss was: ${_fmt_price(rec.stop_loss)}",
        "",
        footer_msg,
        "─" * 32,
        f"#RESULT #{outcome} #{rec.symbol.replace('/', '')} #{'WIN' if is_win else 'LOSS'}",
    ])


# ── Main tracking loop ─────────────────────────────────────────────────────────

async def check_open_signals():
    """
    Called by APScheduler every 60 seconds.
    Fetches live prices for all open signals and closes any that hit TP/SL/expiry.
    """
    open_signals = _get_open_signals()
    if not open_signals:
        logger.debug("Outcome tracker: no open signals to monitor.")
        return

    logger.debug(f"Outcome tracker: monitoring {len(open_signals)} open signal(s)...")

    for rec in open_signals:
        try:
            # ── Check expiry first ────────────────────────────────────────────
            created = rec.created_at
            if created and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)

            expiry_h = EXPIRY_HOURS.get(rec.style, 4)
            if created and (datetime.now(timezone.utc) - created) > timedelta(hours=expiry_h):
                price = await _get_current_price(rec.exchange, rec.symbol, rec.market_type)
                if price:
                    pnl = _calc_expired_pnl(price, rec)
                    _close_signal_in_db(rec.id, "EXPIRED", price, pnl)
                    msg = _build_result_message(rec, "EXPIRED", price, pnl)
                    await send_signal(msg)
                continue

            # ── Fetch live price ──────────────────────────────────────────────
            price = await _get_current_price(rec.exchange, rec.symbol, rec.market_type)
            if price is None:
                continue

            # ── Check TP / SL levels ──────────────────────────────────────────
            outcome = _check_levels(price, rec)
            if outcome is None:
                continue

            pnl = _calc_profit(outcome, rec)
            _close_signal_in_db(rec.id, outcome, price, pnl)
            msg = _build_result_message(rec, outcome, price, pnl)
            await send_signal(msg)

        except Exception as e:
            logger.error(f"Outcome tracker error for signal #{rec.id} ({rec.symbol}): {e}")

    logger.debug("Outcome tracker: check complete.")
