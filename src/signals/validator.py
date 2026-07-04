"""
validator.py — Builds a tradeable signal from a strategy candidate (v3).

Changes vs v2:
  - Structure-based stop loss: behind the last swing point (+ATR buffer),
    never tighter than 1 ATR, rejected if wider than MAX_SL_PCT.
  - Entry is a ZONE around current price sized by ATR; the outcome tracker
    only activates the trade if price actually trades in the zone.
  - TP3 is capped at the next major structural level when one exists.
  - Removed leverage hype ("10-15x") — replaced with risk-based sizing note.
"""
from typing import Optional
from config.settings import (
    MIN_RR_RATIO, TP1_R, TP2_R, TP3_R,
    ATR_SL_BUFFER, MIN_SL_ATR, MAX_SL_PCT, ENTRY_ZONE_ATR,
)
from config.logger import get_logger

logger = get_logger(__name__)


def validate_and_build(cand: dict, style: str = "intraday") -> Optional[dict]:
    direction = cand.get("direction")
    ind = cand.get("indicators", {})
    if not direction:
        return None

    price = ind.get("price")
    atr = ind.get("atr")
    if not price or price <= 0 or not atr:
        return None

    sl_basis = cand.get("sl_basis")

    # ── Stop loss: structural, ATR-buffered ──────────────────────────────────
    if direction == "LONG":
        struct_sl = (sl_basis - ATR_SL_BUFFER * atr) if sl_basis and sl_basis < price else None
        atr_sl = price - MIN_SL_ATR * atr
        stop_loss = min(struct_sl, atr_sl) if struct_sl else atr_sl
        # sanity: structural stop absurdly far → fall back to 1.5 ATR
        if (price - stop_loss) > 3.5 * atr:
            stop_loss = price - 1.5 * atr
        sl_distance = price - stop_loss
    else:
        struct_sl = (sl_basis + ATR_SL_BUFFER * atr) if sl_basis and sl_basis > price else None
        atr_sl = price + MIN_SL_ATR * atr
        stop_loss = max(struct_sl, atr_sl) if struct_sl else atr_sl
        if (stop_loss - price) > 3.5 * atr:
            stop_loss = price + 1.5 * atr
        sl_distance = stop_loss - price

    risk_pct = sl_distance / price * 100
    if risk_pct > MAX_SL_PCT:
        logger.debug(f"Rejected: SL too wide ({risk_pct:.1f}% > {MAX_SL_PCT}%)")
        return None

    # ── Targets ───────────────────────────────────────────────────────────────
    sign = 1 if direction == "LONG" else -1
    tp1 = price + sign * sl_distance * TP1_R
    tp2 = price + sign * sl_distance * TP2_R
    tp3 = price + sign * sl_distance * TP3_R

    # Cap TP3 at major structure if it's closer (be realistic, not greedy)
    if direction == "LONG":
        res = ind.get("nearest_resistance")
        if res and price < res < tp3 and (res - price) >= sl_distance * MIN_RR_RATIO:
            tp3 = res
    else:
        sup = ind.get("nearest_support")
        if sup and tp3 < sup < price and (price - sup) >= sl_distance * MIN_RR_RATIO:
            tp3 = sup

    rr_at_tp2 = abs(tp2 - price) / sl_distance
    if rr_at_tp2 < MIN_RR_RATIO:
        logger.debug(f"Rejected: R:R {rr_at_tp2:.2f} < {MIN_RR_RATIO}")
        return None

    # ── Entry zone (ATR-sized) ────────────────────────────────────────────────
    half = ENTRY_ZONE_ATR * atr
    entry_low, entry_high = price - half, price + half

    d = _decimals(price)
    return {
        "direction":  direction,
        "strategy":   cand.get("strategy"),
        "confidence": cand.get("confidence", 0),
        "reasons":    cand.get("reasons", []),

        "price":      round(price, d),
        "entry_low":  round(entry_low, d),
        "entry_high": round(entry_high, d),

        "tp1": round(tp1, d), "tp2": round(tp2, d), "tp3": round(tp3, d),
        "tp1_pct": round(abs(tp1 - price) / price * 100, 2),
        "tp2_pct": round(abs(tp2 - price) / price * 100, 2),
        "tp3_pct": round(abs(tp3 - price) / price * 100, 2),

        "stop_loss": round(stop_loss, d),
        "risk_pct":  round(risk_pct, 2),
        "rr_ratio":  round(rr_at_tp2, 2),

        "risk_note": _risk_note(risk_pct),

        "atr": round(atr, d),
        "rsi": ind.get("rsi"),
        "adx": ind.get("adx"),
        "vol_ratio": ind.get("vol_ratio"),
        "indicators": ind,
    }


def _risk_note(risk_pct: float) -> str:
    """Position sizing guidance instead of leverage hype."""
    return (f"Risk 1% of account: position = 1% / {risk_pct:.1f}% ≈ "
            f"{100 / risk_pct / 100:.1f}x account size" if risk_pct > 0 else "")


def _decimals(price: float) -> int:
    if price >= 1000: return 2
    if price >= 10:   return 3
    if price >= 1:    return 4
    if price >= 0.01: return 5
    return 8
