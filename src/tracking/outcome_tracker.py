"""
outcome_tracker.py — Monitors all open signals and automatically records
whether TP1 / TP2 / TP3 / SL was hit.

Fixes in this version:
  1. BACKLOG GUARD — old signals (>expiry window) are silently expired
     in bulk without spamming Telegram. Only signals created within
     the last expiry window get result cards sent to the channel.
  2. RESULT RATE LIMIT — result notifications use a separate counter
     from signal alerts, so a burst of closures can't block new signals.
  3. BATCH CLOSE — expired/old signals are bulk-closed in one DB call
     without fetching prices one by one (avoids thundering-herd on API).
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.database.db_logger import SessionLocal, SignalRecord
from src.data.fetcher import fetch_ticker_price
from src.delivery.telegram_bot import send_result          # NEW — dedicated result sender
from config.logger import get_logger

logger = get_logger(__name__)

# Max signal lifetime before silent expiry
EXPIRY_HOURS = {
    "scalp": 2,
    "swing": 72,
}

# Max result notifications per hour (separate from signal rate limit)
MAX_RESULTS_PER_HOUR = 10
_results_this_hour: list = []


# ── Price fetching ─────────────────────────────────────────────────────────────

async def _get_current_price(exchange_name: str, symbol: str, market_type: str) -> Optional[float]:
    """Delegate to fetcher — keeps all ccxt logic in one place."""
    return await fetch_ticker_price(exchange_name, symbol, market_type)


# ── Level checks ──────────────────────────────────────────────────────────────

def _check_levels(price: float, rec: SignalRecord) -> Optional[str]:
    d = rec.direction
    if d == "LONG":
        if rec.tp3 and price >= rec.tp3:             return "TP3"
        if rec.tp2 and price >= rec.tp2:             return "TP2"
        if rec.tp1 and price >= rec.tp1:             return "TP1"
        if rec.stop_loss and price <= rec.stop_loss: return "SL"
    elif d == "SHORT":
        if rec.tp3 and price <= rec.tp3:             return "TP3"
        if rec.tp2 and price <= rec.tp2:             return "TP2"
        if rec.tp1 and price <= rec.tp1:             return "TP1"
        if rec.stop_loss and price >= rec.stop_loss: return "SL"
    return None


def _calc_profit(outcome: str, rec: SignalRecord) -> float:
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


def _calc_mark_to_market(current_price: float, rec: SignalRecord) -> float:
    entry = rec.price_at_signal
    if not entry or entry == 0:
        return 0.0
    if rec.direction == "LONG":
        return round((current_price - entry) / entry * 100, 3)
    else:
        return round((entry - current_price) / entry * 100, 3)


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _close_signal_in_db(signal_id: int, outcome: str, price: float, profit_pct: float):
    with SessionLocal() as db:
        rec = db.get(SignalRecord, signal_id)
        if rec:
            rec.outcome        = outcome
            rec.price_at_close = price
            rec.profit_pct     = profit_pct
            rec.closed_at      = datetime.now(timezone.utc)
            db.commit()
    logger.info(f"Signal #{signal_id} → {outcome}  {profit_pct:+.2f}%")


def _bulk_expire_old_signals(signal_ids: list[int]):
    """
    Silently mark old signals as EXPIRED in one DB pass.
    No price fetch, no Telegram message — just clean the backlog.
    """
    if not signal_ids:
        return
    with SessionLocal() as db:
        db.query(SignalRecord).filter(
            SignalRecord.id.in_(signal_ids)
        ).update(
            {
                "outcome":    "EXPIRED",
                "profit_pct": 0.0,
                "closed_at":  datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
        db.commit()
    logger.info(f"Bulk-expired {len(signal_ids)} stale signals (no Telegram spam).")


def _get_open_signals() -> list[SignalRecord]:
    with SessionLocal() as db:
        recs = db.query(SignalRecord).filter(
            SignalRecord.outcome.is_(None),
            SignalRecord.sent_to_telegram.is_(True),
        ).all()
        db.expunge_all()
        return recs


def _is_within_notify_window(rec: SignalRecord) -> bool:
    """
    Return True only if the signal is recent enough that a Telegram
    result card is still useful to subscribers.
    We only notify for signals created in the last expiry window.
    """
    created = rec.created_at
    if not created:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    expiry_h = EXPIRY_HOURS.get(rec.style, 4)
    age = datetime.now(timezone.utc) - created
    return age <= timedelta(hours=expiry_h)


# ── Result rate limiter (separate from signal rate limit) ──────────────────────

async def _send_result_guarded(text: str):
    """
    Send a result card respecting a separate MAX_RESULTS_PER_HOUR limit.
    This prevents a burst of closures from blocking new signal alerts.
    """
    import time
    now = time.time()
    _results_this_hour[:] = [t for t in _results_this_hour if now - t < 3600]
    if len(_results_this_hour) >= MAX_RESULTS_PER_HOUR:
        logger.debug("Result rate limit reached — result card suppressed.")
        return
    await send_result(text)
    _results_this_hour.append(now)


# ── Telegram result card ───────────────────────────────────────────────────────

def _fmt_price(value: Optional[float]) -> str:
    if value is None: return "N/A"
    if value >= 1000:   return f"{value:,.2f}"
    elif value >= 1:    return f"{value:.4f}"
    elif value >= 0.01: return f"{value:.5f}"
    else:               return f"{value:.8f}"


def _elapsed(rec: SignalRecord) -> str:
    created = rec.created_at
    if not created: return "?"
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - created
    total_min = int(delta.total_seconds() / 60)
    if total_min < 60: return f"{total_min}m"
    h, m = divmod(total_min, 60)
    return f"{h}h {m}m"


def _build_result_message(rec: SignalRecord, outcome: str, exit_price: float, profit_pct: float) -> str:
    is_win  = outcome in ("TP1", "TP2", "TP3")
    is_expr = outcome == "EXPIRED"

    emoji_map = {"TP3": "🏆", "TP2": "✅", "TP1": "✅", "SL": "❌", "EXPIRED": "⏰"}
    label_map = {
        "TP3": "FULL TARGET HIT", "TP2": "TARGET 2 HIT",
        "TP1": "TARGET 1 HIT",   "SL":  "STOPPED OUT", "EXPIRED": "SIGNAL EXPIRED",
    }
    result_emoji = emoji_map.get(outcome, "⚪")
    result_label = label_map.get(outcome, outcome)
    dir_emoji    = "🟢" if rec.direction == "LONG" else "🔴"
    style_emoji  = "⚡" if rec.style == "scalp" else "📈"
    sign         = "+" if profit_pct >= 0 else ""
    pnl_emoji    = "💚" if profit_pct >= 0 else "🔴"
    footer_msg   = (
        "🟢 Profitable trade!" if is_win else
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
        f"📍 Entry:    ${_fmt_price(rec.price_at_signal)}",
        f"📍 Exit:     ${_fmt_price(exit_price)}",
        f"{pnl_emoji} P&amp;L:     <b>{sign}{profit_pct:.2f}%</b>",
        f"⏱ Duration: {_elapsed(rec)}",
        "",
        f"🎯 TP1 ${_fmt_price(rec.tp1)}  TP2 ${_fmt_price(rec.tp2)}  TP3 ${_fmt_price(rec.tp3)}",
        f"🛡 SL ${_fmt_price(rec.stop_loss)}",
        "",
        footer_msg,
        "─" * 32,
        f"#RESULT #{outcome} #{rec.symbol.replace('/', '')} #{'WIN' if is_win else 'LOSS'}",
    ])


# ── Main tracking loop ─────────────────────────────────────────────────────────

async def check_open_signals():
    """
    Called by APScheduler every 60 seconds.

    Step 1: Silently bulk-expire any signals older than their expiry window
            (no Telegram, no price fetch — just cleans the backlog instantly).
    Step 2: For remaining active signals, fetch prices and check TP/SL.
            Only send Telegram result cards for signals within notify window.
    """
    open_signals = _get_open_signals()
    if not open_signals:
        logger.debug("Outcome tracker: no open signals.")
        return

    now = datetime.now(timezone.utc)

    # ── Step 1: Split into stale vs active ────────────────────────────────────
    stale_ids = []
    active    = []

    for rec in open_signals:
        created = rec.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        expiry_h = EXPIRY_HOURS.get(rec.style, 4)
        if created and (now - created) > timedelta(hours=expiry_h):
            stale_ids.append(rec.id)
        else:
            active.append(rec)

    # Bulk-expire stale signals silently (no Telegram flood)
    if stale_ids:
        _bulk_expire_old_signals(stale_ids)
        logger.info(f"Outcome tracker: {len(stale_ids)} stale signals expired silently, "
                    f"{len(active)} active signals to monitor.")

    if not active:
        return

    logger.debug(f"Outcome tracker: checking {len(active)} active signal(s)...")

    # ── Step 2: Check active signals ──────────────────────────────────────────
    for rec in active:
        try:
            price = await _get_current_price(rec.exchange, rec.symbol, rec.market_type)
            if price is None:
                continue

            outcome = _check_levels(price, rec)
            if outcome is None:
                continue

            pnl = _calc_profit(outcome, rec)
            _close_signal_in_db(rec.id, outcome, price, pnl)

            # Only notify Telegram for recent signals (within notify window)
            if _is_within_notify_window(rec):
                msg = _build_result_message(rec, outcome, price, pnl)
                await _send_result_guarded(msg)
            else:
                logger.debug(f"Signal #{rec.id} closed silently (outside notify window).")

        except Exception as e:
            logger.error(f"Outcome tracker error for signal #{rec.id} ({rec.symbol}): {e}")

    logger.debug("Outcome tracker: check complete.")
