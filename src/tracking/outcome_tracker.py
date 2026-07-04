"""
outcome_tracker.py — Candle-based trade lifecycle tracking (v3).

Why this is a full rewrite:
  v2 polled the *last ticker price* every 60s, so any TP/SL touched by a
  wick between polls was silently missed, and every signal was assumed to
  be filled instantly at scan price. v2 also never posted SL results, so
  the channel history looked like the bot never lost.

v3:
  - PENDING → ACTIVE: a trade only becomes active if a 1m candle actually
    trades through the entry zone. If price runs to TP without filling,
    the signal closes as NOFILL (no win claimed, no loss booked).
  - Level detection walks 1m candle highs/lows since the last check —
    wicks count. If a candle touches both SL and TP, SL is assumed first
    (pessimistic, honest).
  - Scaled exit model for R accounting: 1/3 closed at TP1 (SL→breakeven),
    1/3 at TP2 (SL→TP1), 1/3 rides to TP3 or the trailed stop.
  - ALL outcomes are posted to the channel — wins AND losses.
  - MFE/MAE recorded for every trade (feeds the ML model and honest stats).
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd

from src.database.db_logger import SessionLocal, SignalRecord
from src.data.fetcher import fetch_ohlcv, fetch_ticker_price
from src.delivery.telegram_bot import send_result
from config.settings import (
    FILL_EXPIRY_HOURS, TRADE_EXPIRY_HOURS, TP1_R, TP2_R, TP3_R, TP_PORTIONS,
)
from config.logger import get_logger

logger = get_logger(__name__)

MAX_RESULTS_PER_HOUR = 30
_results_this_hour: list = []


# ── Candle data ───────────────────────────────────────────────────────────────

async def _get_candles_since(rec: SignalRecord, since: datetime) -> Optional[pd.DataFrame]:
    """1m candles since `since` (falls back to 5m for long gaps)."""
    age_min = (datetime.now(timezone.utc) - since).total_seconds() / 60
    tf, limit = ("1m", min(int(age_min) + 3, 900)) if age_min <= 850 else ("5m", min(int(age_min / 5) + 3, 900))
    df = await fetch_ohlcv(rec.exchange, rec.symbol, tf, rec.market_type,
                           limit=max(limit, 5), use_cache=False)
    if df is None:
        return None
    return df[df.index >= since]


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ── R / P&L accounting (scaled exit model) ────────────────────────────────────

def _realized(rec: SignalRecord, final_exit: float) -> tuple[float, float]:
    """
    Returns (r_multiple, profit_pct) under the scaled exit model,
    given the final exit price of the remaining position.
    """
    entry = rec.fill_price or rec.price_at_signal
    risk = abs(entry - rec.stop_loss)
    if not entry or not risk:
        return 0.0, 0.0
    sign = 1 if rec.direction == "LONG" else -1
    p1, p2, p3 = TP_PORTIONS

    exits = []
    if rec.highest_tp_hit in ("TP1", "TP2", "TP3"):
        exits.append((p1, rec.tp1))
    if rec.highest_tp_hit in ("TP2", "TP3"):
        exits.append((p2, rec.tp2))
    if rec.highest_tp_hit == "TP3":
        exits.append((p3, rec.tp3))
    # remaining portion exits at final_exit
    used = sum(p for p, _ in exits)
    if used < 0.999:
        exits.append((1 - used, final_exit))

    r_total = sum(p * (sign * (px - entry) / risk) for p, px in exits if px)
    pnl_total = sum(p * (sign * (px - entry) / entry * 100) for p, px in exits if px)
    return round(r_total, 3), round(pnl_total, 3)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _update(rec_id: int, **kwargs):
    with SessionLocal() as db:
        r = db.get(SignalRecord, rec_id)
        if r:
            for k, v in kwargs.items():
                setattr(r, k, v)
            db.commit()


def _close(rec: SignalRecord, outcome: str, exit_price: float):
    r_mult, pnl = _realized(rec, exit_price)
    _update(rec.id, outcome=outcome, status="CLOSED", price_at_close=exit_price,
            profit_pct=pnl, r_multiple=r_mult,
            closed_at=datetime.now(timezone.utc))
    logger.info(f"Signal #{rec.id} {rec.symbol} → {outcome}  R={r_mult:+.2f}  {pnl:+.2f}%")
    return r_mult, pnl


def _get_open() -> list[SignalRecord]:
    with SessionLocal() as db:
        recs = db.query(SignalRecord).filter(
            SignalRecord.outcome.is_(None),
            SignalRecord.sent_to_telegram.is_(True),
        ).all()
        db.expunge_all()
        return recs


# ── Level walking ─────────────────────────────────────────────────────────────

def _walk_pending(rec: SignalRecord, candles: pd.DataFrame):
    """
    Walk candles for a PENDING signal.
    Returns (filled_index, fill_price) or ("NOFILL_TP", None) if price ran
    to TP1 without ever filling, or (None, None) if still pending.
    """
    lo, hi = rec.entry_low, rec.entry_high
    for i, (ts, c) in enumerate(candles.iterrows()):
        overlaps = c["low"] <= hi and c["high"] >= lo
        if overlaps:
            # fill at zone boundary or open, whichever is inside the zone
            fill = min(max(c["open"], lo), hi)
            return i, float(fill)
        if rec.direction == "LONG" and rec.tp1 and c["high"] >= rec.tp1:
            return "NOFILL_TP", None
        if rec.direction == "SHORT" and rec.tp1 and c["low"] <= rec.tp1:
            return "NOFILL_TP", None
    return None, None


def _walk_active(rec: SignalRecord, candles: pd.DataFrame, state: dict):
    """
    Walk candles for an ACTIVE trade. Mutates `state`:
      highest_tp_hit, adjusted_sl, mfe, mae
    Returns (outcome, exit_price) or (None, None).
    Pessimistic rule: SL checked before TP within each candle.
    """
    sign = 1 if rec.direction == "LONG" else -1
    entry = state["fill_price"]

    for ts, c in candles.iterrows():
        hi_ex = sign * (c["high"] if sign == 1 else c["low"]) - sign * entry   # favorable
        lo_ex = sign * entry - sign * (c["low"] if sign == 1 else c["high"])   # adverse
        state["mfe"] = max(state["mfe"], hi_ex / entry * 100)
        state["mae"] = max(state["mae"], lo_ex / entry * 100)

        eff_sl = state["adjusted_sl"] if state["adjusted_sl"] is not None else rec.stop_loss

        sl_touched = (c["low"] <= eff_sl) if sign == 1 else (c["high"] >= eff_sl)
        if sl_touched:
            if state["highest_tp_hit"] is None:
                return "SL", float(eff_sl)
            # stopped after partials — outcome is the highest TP reached
            return state["highest_tp_hit"], float(eff_sl)

        def hit(level):
            return level and ((c["high"] >= level) if sign == 1 else (c["low"] <= level))

        if hit(rec.tp3):
            state["highest_tp_hit"] = "TP3"
            return "TP3", float(rec.tp3)
        if hit(rec.tp2) and state["highest_tp_hit"] != "TP2":
            state["highest_tp_hit"] = "TP2"
            state["adjusted_sl"] = rec.tp1
        elif hit(rec.tp1) and state["highest_tp_hit"] is None:
            state["highest_tp_hit"] = "TP1"
            state["adjusted_sl"] = entry   # breakeven

    return None, None


# ── Result posting (ALL results — wins and losses) ────────────────────────────

def _fmt(v: Optional[float]) -> str:
    if v is None: return "N/A"
    if v >= 1000: return f"{v:,.2f}"
    if v >= 1:    return f"{v:.4f}"
    if v >= 0.01: return f"{v:.5f}"
    return f"{v:.8f}"


def _elapsed(rec: SignalRecord) -> str:
    start = _aware(rec.filled_at or rec.created_at)
    if not start: return "?"
    m = int((datetime.now(timezone.utc) - start).total_seconds() / 60)
    return f"{m}m" if m < 60 else f"{m//60}h {m%60}m"


def _result_msg(rec: SignalRecord, outcome: str, exit_price: float,
                r_mult: float, pnl: float) -> str:
    emoji = {"TP3": "🏆", "TP2": "✅", "TP1": "✅", "SL": "❌",
             "NOFILL": "⚪", "EXPIRED": "⏰"}.get(outcome, "⚪")
    label = {"TP3": "FULL TARGET HIT", "TP2": "TP2 + TRAILED OUT",
             "TP1": "TP1 + TRAILED OUT", "SL": "STOPPED OUT",
             "NOFILL": "ENTRY NOT FILLED", "EXPIRED": "EXPIRED"}.get(outcome, outcome)
    dir_e = "🟢" if rec.direction == "LONG" else "🔴"
    sign = "+" if r_mult >= 0 else ""

    lines = [
        "─" * 30,
        f"{emoji} <b>RESULT — {label}</b>",
        "─" * 30,
        f"{dir_e} <b>{rec.symbol}</b> {rec.direction} · {rec.style.upper()} · {rec.strategy or ''}",
    ]
    if outcome == "NOFILL":
        lines += ["", "Price never reached the entry zone — no trade, no P&L.",
                  "(We only count trades that actually filled.)"]
    else:
        lines += [
            "",
            f"📍 Entry: ${_fmt(rec.fill_price or rec.price_at_signal)}   Exit: ${_fmt(exit_price)}",
            f"💰 Result: <b>{sign}{r_mult:.2f}R</b>  ({sign}{pnl:.2f}%)",
            f"⏱ Duration: {_elapsed(rec)}",
        ]
        if rec.highest_tp_hit:
            lines.append(f"🔄 Scaled exits: {rec.highest_tp_hit} reached, remainder trailed")
    tag = "WIN" if r_mult > 0.05 else ("LOSS" if r_mult < -0.05 else "FLAT")
    lines += ["─" * 30, f"#RESULT #{outcome} #{rec.symbol.replace('/', '')} #{tag}"]
    return "\n".join(lines)


async def _post(text: str):
    import time
    now = time.time()
    _results_this_hour[:] = [t for t in _results_this_hour if now - t < 3600]
    if len(_results_this_hour) >= MAX_RESULTS_PER_HOUR:
        return
    await send_result(text)
    _results_this_hour.append(now)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def check_open_signals():
    open_signals = _get_open()
    if not open_signals:
        return

    now = datetime.now(timezone.utc)

    for rec in open_signals:
        try:
            created = _aware(rec.created_at)
            since = _aware(rec.last_checked_at) or created
            candles = await _get_candles_since(rec, since)
            if candles is None or candles.empty:
                _update(rec.id, last_checked_at=now)
                continue

            status = rec.status or ("ACTIVE" if rec.fill_price else "PENDING")

            # ── PENDING: look for a fill ─────────────────────────────────────
            if status == "PENDING":
                fill_idx, fill_price = _walk_pending(rec, candles)

                if fill_idx == "NOFILL_TP":
                    _close(rec, "NOFILL", rec.price_at_signal)
                    await _post(_result_msg(rec, "NOFILL", rec.price_at_signal, 0, 0))
                    continue

                if fill_idx is None:
                    expiry = FILL_EXPIRY_HOURS.get(rec.style, 4)
                    if created and (now - created) > timedelta(hours=expiry):
                        _close(rec, "NOFILL", rec.price_at_signal)
                    else:
                        _update(rec.id, last_checked_at=now)
                    continue

                # Filled
                filled_at = candles.index[fill_idx].to_pydatetime()
                _update(rec.id, status="ACTIVE", fill_price=fill_price, filled_at=filled_at)
                rec.status, rec.fill_price, rec.filled_at = "ACTIVE", fill_price, filled_at
                candles = candles.iloc[fill_idx:]
                status = "ACTIVE"

            # ── ACTIVE: walk levels ──────────────────────────────────────────
            if status == "ACTIVE":
                state = {
                    "fill_price": rec.fill_price or rec.price_at_signal,
                    "highest_tp_hit": rec.highest_tp_hit,
                    "adjusted_sl": rec.adjusted_sl,
                    "mfe": rec.mfe_pct or 0.0,
                    "mae": rec.mae_pct or 0.0,
                }
                outcome, exit_price = _walk_active(rec, candles, state)

                # persist trailing / excursion state
                _update(rec.id, highest_tp_hit=state["highest_tp_hit"],
                        adjusted_sl=state["adjusted_sl"],
                        mfe_pct=round(state["mfe"], 3), mae_pct=round(state["mae"], 3),
                        last_checked_at=now)
                rec.highest_tp_hit = state["highest_tp_hit"]
                rec.adjusted_sl = state["adjusted_sl"]

                if outcome:
                    r_mult, pnl = _close(rec, outcome, exit_price)
                    await _post(_result_msg(rec, outcome, exit_price, r_mult, pnl))
                    continue

                # Trade expiry: force-close at market
                started = _aware(rec.filled_at) or created
                expiry = TRADE_EXPIRY_HOURS.get(rec.style, 48)
                if started and (now - started) > timedelta(hours=expiry):
                    price = await fetch_ticker_price(rec.exchange, rec.symbol, rec.market_type)
                    if price:
                        r_mult, pnl = _close(rec, "EXPIRED", price)
                        await _post(_result_msg(rec, "EXPIRED", price, r_mult, pnl))

        except Exception as e:
            logger.error(f"Outcome tracker error #{rec.id} ({rec.symbol}): {e}")
