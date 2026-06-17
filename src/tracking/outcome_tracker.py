"""
outcome_tracker.py — Monitors all open signals and automatically records
whether TP1 / TP2 / TP3 / SL was hit.

v2.0 — Trailing stop management:
  - When price reaches 50% of TP1 → move SL to break-even
  - When TP1 hit → move SL to 50% between entry and TP1, send notification
  - When TP2 hit → move SL to TP1, send notification
  - When TP3 hit → close fully
  - SL check uses adjusted_sl if set, otherwise original stop_loss

Also includes:
  - Backlog guard for stale signals
  - Result rate limiting
  - Batch expiry of old signals
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.database.db_logger import SessionLocal, SignalRecord
from src.data.fetcher import fetch_ticker_price
from src.delivery.telegram_bot import send_result
from config.settings import TRAILING_STOP_ENABLED, BREAKEVEN_TRIGGER
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
    return await fetch_ticker_price(exchange_name, symbol, market_type)


# ── Trailing stop logic ──────────────────────────────────────────────────────

def _get_effective_sl(rec: SignalRecord) -> float:
    """Return the adjusted SL if set, otherwise the original."""
    return rec.adjusted_sl if rec.adjusted_sl is not None else rec.stop_loss


def _check_trailing_and_levels(price: float, rec: SignalRecord) -> Optional[str]:
    """
    Check TP/SL levels with trailing stop management.

    Returns:
      - "TP3" / "TP2" / "TP1" / "SL" if a level is definitively hit
      - "TRAIL_BE" / "TRAIL_TP1" / "TRAIL_TP2" if trailing stop should be moved
      - None if no level hit
    """
    d = rec.direction
    entry = rec.price_at_signal
    effective_sl = _get_effective_sl(rec)

    if d == "LONG":
        # Check definitive closes first
        if rec.tp3 and price >= rec.tp3:
            return "TP3"
        if effective_sl and price <= effective_sl:
            return "SL"

        # Check trailing stop moves (only if enabled)
        if TRAILING_STOP_ENABLED and entry:
            # TP2 hit → trail SL to TP1
            if rec.tp2 and price >= rec.tp2 and rec.highest_tp_hit != "TP2":
                return "TRAIL_TP2"
            # TP1 hit → trail SL to midpoint(entry, TP1)
            if rec.tp1 and price >= rec.tp1 and rec.highest_tp_hit not in ("TP1", "TP2"):
                return "TRAIL_TP1"
            # Break-even trigger → move SL to entry
            if rec.tp1 and rec.highest_tp_hit is None:
                be_price = entry + (rec.tp1 - entry) * BREAKEVEN_TRIGGER
                if price >= be_price and rec.adjusted_sl is None:
                    return "TRAIL_BE"

    elif d == "SHORT":
        if rec.tp3 and price <= rec.tp3:
            return "TP3"
        if effective_sl and price >= effective_sl:
            return "SL"

        if TRAILING_STOP_ENABLED and entry:
            if rec.tp2 and price <= rec.tp2 and rec.highest_tp_hit != "TP2":
                return "TRAIL_TP2"
            if rec.tp1 and price <= rec.tp1 and rec.highest_tp_hit not in ("TP1", "TP2"):
                return "TRAIL_TP1"
            if rec.tp1 and rec.highest_tp_hit is None:
                be_price = entry - (entry - rec.tp1) * BREAKEVEN_TRIGGER
                if price <= be_price and rec.adjusted_sl is None:
                    return "TRAIL_BE"

    return None


def _apply_trailing_move(signal_id: int, trail_type: str, rec: SignalRecord):
    """Update the adjusted SL and highest_tp_hit in the database."""
    entry = rec.price_at_signal
    d = rec.direction

    with SessionLocal() as db:
        record = db.get(SignalRecord, signal_id)
        if not record:
            return

        if trail_type == "TRAIL_BE":
            # Move SL to break-even (entry price)
            record.adjusted_sl = entry
            logger.info(f"Signal #{signal_id}: SL moved to break-even ${entry}")

        elif trail_type == "TRAIL_TP1":
            # TP1 hit → move SL to midpoint between entry and TP1
            record.highest_tp_hit = "TP1"
            if d == "LONG":
                new_sl = entry + (rec.tp1 - entry) * 0.5
            else:
                new_sl = entry - (entry - rec.tp1) * 0.5
            record.adjusted_sl = round(new_sl, 8)
            record.partial_profit_pct = round(
                abs(rec.tp1 - entry) / entry * 100, 2
            )
            logger.info(f"Signal #{signal_id}: TP1 hit! SL trailed to ${new_sl:.6f}")

        elif trail_type == "TRAIL_TP2":
            # TP2 hit → move SL to TP1
            record.highest_tp_hit = "TP2"
            record.adjusted_sl = rec.tp1
            record.partial_profit_pct = round(
                abs(rec.tp2 - entry) / entry * 100, 2
            )
            logger.info(f"Signal #{signal_id}: TP2 hit! SL trailed to TP1 ${rec.tp1}")

        db.commit()


# ── Profit calculation ────────────────────────────────────────────────────────

def _calc_profit(outcome: str, rec: SignalRecord) -> float:
    entry = rec.price_at_signal
    if not entry or entry == 0:
        return 0.0

    # For SL with trailing stop: use adjusted SL for actual exit price
    if outcome == "SL" and rec.adjusted_sl is not None:
        target = rec.adjusted_sl
    else:
        target_map = {"TP1": rec.tp1, "TP2": rec.tp2, "TP3": rec.tp3, "SL": rec.stop_loss}
        target = target_map.get(outcome)

    if not target:
        return 0.0
    if rec.direction == "LONG":
        return round((target - entry) / entry * 100, 3)
    else:
        return round((entry - target) / entry * 100, 3)


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
    logger.info(f"Bulk-expired {len(signal_ids)} stale signals.")


def _get_open_signals() -> list[SignalRecord]:
    with SessionLocal() as db:
        recs = db.query(SignalRecord).filter(
            SignalRecord.outcome.is_(None),
            SignalRecord.sent_to_telegram.is_(True),
        ).all()
        db.expunge_all()
        return recs


def _is_within_notify_window(rec: SignalRecord) -> bool:
    created = rec.created_at
    if not created:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    expiry_h = EXPIRY_HOURS.get(rec.style, 4)
    age = datetime.now(timezone.utc) - created
    return age <= timedelta(hours=expiry_h)


# ── Result rate limiter ────────────────────────────────────────────────────────

async def _send_result_guarded(text: str):
    import time
    now = time.time()
    _results_this_hour[:] = [t for t in _results_this_hour if now - t < 3600]
    if len(_results_this_hour) >= MAX_RESULTS_PER_HOUR:
        logger.debug("Result rate limit reached — result card suppressed.")
        return
    await send_result(text)
    _results_this_hour.append(now)


# ── Telegram result / trailing notification ────────────────────────────────────

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

    # SL with trailing: might actually be profitable
    if outcome == "SL" and profit_pct > 0:
        footer_msg = "🟢 Stopped out in profit (trailing stop)."
    elif is_win:
        footer_msg = "🟢 Profitable trade!"
    elif is_expr:
        footer_msg = "⏰ Expired without hitting TP or SL."
    else:
        footer_msg = "🔴 Stopped out. Risk managed."

    # Show if trailing stop was active
    trail_info = ""
    if rec.highest_tp_hit:
        trail_info = f"\n🔄 Trailing: {rec.highest_tp_hit} was hit, SL was trailed"
    if rec.adjusted_sl and rec.adjusted_sl != rec.stop_loss:
        trail_info += f"\n🛡 Adjusted SL: ${_fmt_price(rec.adjusted_sl)}"

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
        trail_info,
        "",
        footer_msg,
        "─" * 32,
        f"#RESULT #{outcome} #{rec.symbol.replace('/', '')} #{'WIN' if is_win or profit_pct > 0 else 'LOSS'}",
    ])


def _build_trailing_notification(rec: SignalRecord, trail_type: str, new_sl: float) -> str:
    """Build a short notification for trailing stop moves."""
    dir_emoji = "🟢" if rec.direction == "LONG" else "🔴"

    if trail_type == "TRAIL_BE":
        label = "SL → BREAK-EVEN"
        detail = "Risk eliminated! 🔒"
    elif trail_type == "TRAIL_TP1":
        label = "TP1 HIT — SL TRAILED"
        detail = "Profit locked! 🎯"
    elif trail_type == "TRAIL_TP2":
        label = "TP2 HIT — SL → TP1"
        detail = "Major profit secured! 🏆"
    else:
        label = "SL ADJUSTED"
        detail = ""

    return "\n".join([
        "─" * 32,
        f"🔄 <b>{label}</b>",
        "─" * 32,
        f"{dir_emoji} <b>{rec.symbol}</b>  ·  {rec.direction}  ·  {rec.style.upper()}",
        f"🛡 New SL: <b>${_fmt_price(new_sl)}</b>",
        f"📍 Entry: ${_fmt_price(rec.price_at_signal)}",
        detail,
        "─" * 32,
    ])


# ── Main tracking loop ─────────────────────────────────────────────────────────

async def check_open_signals():
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

    if stale_ids:
        _bulk_expire_old_signals(stale_ids)
        logger.info(f"Outcome tracker: {len(stale_ids)} stale signals expired, "
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

            result = _check_trailing_and_levels(price, rec)
            if result is None:
                continue

            # Handle trailing stop moves (not a close — just SL adjustment)
            if result.startswith("TRAIL_"):
                _apply_trailing_move(rec.id, result, rec)

                # Send notification for TP hits (not for break-even, too noisy)
                if result in ("TRAIL_TP1", "TRAIL_TP2") and _is_within_notify_window(rec):
                    # Get the new SL after the trailing move
                    with SessionLocal() as db:
                        updated_rec = db.get(SignalRecord, rec.id)
                        new_sl = updated_rec.adjusted_sl if updated_rec else rec.price_at_signal
                    msg = _build_trailing_notification(rec, result, new_sl)
                    await _send_result_guarded(msg)
                continue

            # Handle definitive closes (TP3, SL)
            pnl = _calc_profit(result, rec)
            _close_signal_in_db(rec.id, result, price, pnl)

            if _is_within_notify_window(rec):
                if result == "SL":
                    logger.debug(f"Signal #{rec.id} closed silently (SL hit).")
                else:
                    msg = _build_result_message(rec, result, price, pnl)
                    await _send_result_guarded(msg)
            else:
                logger.debug(f"Signal #{rec.id} closed silently (outside notify window).")

        except Exception as e:
            logger.error(f"Outcome tracker error for signal #{rec.id} ({rec.symbol}): {e}")

    logger.debug("Outcome tracker: check complete.")
