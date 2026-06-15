"""
validator.py — Validates a signal, computes entry zone, TP levels, and stop loss.
v2.0: Separate SL multiplier for swing vs scalp, counter-trend blocking,
      style-aware validation.

Uses ATR for dynamic stop placement. Enforces minimum R:R ratio.
"""
from typing import Optional
from config.settings import (
    ATR_SL_MULTIPLIER, ATR_SL_MULTIPLIER_SWING,
    MIN_RR_RATIO, MIN_CONFIDENCE, MIN_INDICATORS_AGREE,
    TP1_R, TP2_R, TP3_R,
    COUNTER_TREND_BLOCK,
)
from config.logger import get_logger

logger = get_logger(__name__)


def validate_and_build(
    score_result: dict,
    market_type: str = "spot",
    style: str = "scalp",
) -> Optional[dict]:
    """
    Takes the output of score_scalp / score_swing and:
    1. Checks confidence threshold
    2. Checks minimum indicator agreement
    3. Applies counter-trend guard (EMA200)
    4. Computes entry, TP1/TP2/TP3, stop loss (style-aware SL multiplier)
    5. Checks minimum R:R ratio
    Returns a complete signal dict or None if it fails validation.
    """
    direction  = score_result.get("direction")
    confidence = score_result.get("confidence", 0)
    reasons    = score_result.get("reasons", [])
    ind        = score_result.get("indicators", {})

    if not direction:
        return None

    if confidence < MIN_CONFIDENCE:
        logger.info(f"Signal rejected: Confidence {confidence} < {MIN_CONFIDENCE}")
        return None

    if len(reasons) < MIN_INDICATORS_AGREE:
        logger.info(f"Signal rejected: len(reasons) {len(reasons)} < {MIN_INDICATORS_AGREE}")
        return None

    price = ind.get("price")
    atr   = ind.get("atr")

    if not price or price <= 0:
        logger.info(f"Signal rejected: invalid price {price}")
        return None

    # ── Spot market: SHORT not possible ───────────────────────────────────────
    if market_type == "spot" and direction == "SHORT":
        logger.info(f"Signal rejected: cannot SHORT on spot market")
        return None

    # ── Counter-trend block ───────────────────────────────────────────────────
    # Reject signals that go against the EMA200 macro trend
    if COUNTER_TREND_BLOCK and ind.get("above_200") is not None:
        if direction == "LONG" and ind["above_200"] is False:
            logger.info(f"Signal rejected: LONG but price < EMA200 ({price} < {ind.get('ema200', '?')})")
            return None
        if direction == "SHORT" and ind["above_200"] is True:
            logger.info(f"Signal rejected: SHORT but price > EMA200 ({price} > {ind.get('ema200', '?')})")
            return None

    # ── Stop loss using ATR (style-aware multiplier) ──────────────────────────
    if style == "swing":
        sl_mult = ATR_SL_MULTIPLIER_SWING
    else:
        sl_mult = ATR_SL_MULTIPLIER

    sl_distance = (atr * sl_mult) if atr else (price * 0.02)

    # Entry zone width: slightly wider for swing trades
    if style == "swing":
        entry_spread = 0.002  # ±0.2%
    else:
        entry_spread = 0.001  # ±0.1%

    if direction == "LONG":
        entry_low  = round(price * (1 - entry_spread), _decimals(price))
        entry_high = round(price * (1 + entry_spread), _decimals(price))
        stop_loss  = round(price - sl_distance, _decimals(price))
        tp1        = round(price + sl_distance * TP1_R, _decimals(price))
        tp2        = round(price + sl_distance * TP2_R, _decimals(price))
        tp3        = round(price + sl_distance * TP3_R, _decimals(price))
        risk_pct   = (price - stop_loss) / price * 100
        tp1_pct    = (tp1 - price) / price * 100
        tp2_pct    = (tp2 - price) / price * 100
        tp3_pct    = (tp3 - price) / price * 100
    else:  # SHORT
        entry_low  = round(price * (1 - entry_spread), _decimals(price))
        entry_high = round(price * (1 + entry_spread), _decimals(price))
        stop_loss  = round(price + sl_distance, _decimals(price))
        tp1        = round(price - sl_distance * TP1_R, _decimals(price))
        tp2        = round(price - sl_distance * TP2_R, _decimals(price))
        tp3        = round(price - sl_distance * TP3_R, _decimals(price))
        risk_pct   = (stop_loss - price) / price * 100
        tp1_pct    = (price - tp1) / price * 100
        tp2_pct    = (price - tp2) / price * 100
        tp3_pct    = (price - tp3) / price * 100

    # R:R at TP2 (the conservative target)
    rr_ratio = tp2_pct / risk_pct if risk_pct else 0

    if rr_ratio < MIN_RR_RATIO:
        logger.info(f"Signal rejected: R:R {rr_ratio:.2f} < {MIN_RR_RATIO}")
        return None

    # ── Suggested leverage ────────────────────────────────────────────────────
    leverage = _suggest_leverage(confidence, ind.get("atr_pct"), market_type)

    return {
        "direction":   direction,
        "confidence":  confidence,
        "reasons":     reasons,

        "price":       price,
        "entry_low":   entry_low,
        "entry_high":  entry_high,

        "tp1":         tp1,
        "tp2":         tp2,
        "tp3":         tp3,
        "tp1_pct":     round(tp1_pct, 2),
        "tp2_pct":     round(tp2_pct, 2),
        "tp3_pct":     round(tp3_pct, 2),

        "stop_loss":   stop_loss,
        "risk_pct":    round(risk_pct, 2),
        "rr_ratio":    round(rr_ratio, 2),

        "leverage":    leverage,

        "atr":         round(atr, _decimals(price)) if atr else None,
        "rsi":         ind.get("rsi"),
        "adx":         ind.get("adx"),
        "vol_ratio":   ind.get("vol_ratio"),
        "above_200":   ind.get("above_200"),
    }


def _suggest_leverage(confidence: float, atr_pct: Optional[float], market_type: str) -> str:
    """Return a human-readable leverage suggestion."""
    if market_type == "spot":
        return "1x (spot — no leverage)"

    if atr_pct and atr_pct > 5:
        return "2–3x (high volatility)"
    elif atr_pct and atr_pct > 2:
        if confidence >= 85:
            return "5–10x"
        return "3–5x"
    else:
        if confidence >= 85:
            return "10–15x"
        return "5–10x"


def _decimals(price: float) -> int:
    """Return appropriate decimal places for a price."""
    if price >= 1000:
        return 2
    elif price >= 10:
        return 3
    elif price >= 1:
        return 4
    elif price >= 0.01:
        return 5
    else:
        return 8
