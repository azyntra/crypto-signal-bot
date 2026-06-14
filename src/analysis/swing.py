"""
swing.py — Swing trading strategy for 1h / 4h / 1d timeframes.
Uses trend-following signals: EMA alignment, ADX trend strength, MACD,
RSI momentum, OBV confirmation, and higher-timeframe structure.
"""
from typing import Optional
from config.settings import RSI_OVERSOLD, RSI_OVERBOUGHT, ADX_TREND_MIN
from config.logger import get_logger

logger = get_logger(__name__)

SWING_WEIGHTS = {
    "ema_trend":  20,
    "adx":        20,
    "macd":       20,
    "rsi":        15,
    "obv":        15,
    "ema200":     10,
}


def score_swing(
    ind_base: Optional[dict],   # primary TF (e.g. 1h or 4h)
    ind_high: Optional[dict],   # higher TF confirmation (e.g. 4h or 1d)
) -> dict:
    """
    Score a swing trading opportunity using multi-timeframe confluence.
    Returns same structure as score_scalp.
    """
    if not ind_base:
        return _empty()

    long_score  = 0.0
    short_score = 0.0
    long_reasons  = []
    short_reasons = []

    ind = ind_base

    # ── EMA trend alignment (weight 20) ───────────────────────────────────────
    if ind.get("ema_bull"):
        long_score += SWING_WEIGHTS["ema_trend"]
        long_reasons.append("EMA 9>21>50 bull alignment")
    elif ind.get("ema_bear"):
        short_score += SWING_WEIGHTS["ema_trend"]
        short_reasons.append("EMA 9<21<50 bear alignment")
    # partial: price above/below EMA50
    elif ind.get("price") and ind.get("ema50"):
        if ind["price"] > ind["ema50"]:
            long_score  += SWING_WEIGHTS["ema_trend"] * 0.5
        else:
            short_score += SWING_WEIGHTS["ema_trend"] * 0.5

    # ── ADX trend strength (weight 20) ────────────────────────────────────────
    adx = ind.get("adx")
    if adx and adx > ADX_TREND_MIN:
        if ind.get("adx_bull"):
            long_score += SWING_WEIGHTS["adx"]
            long_reasons.append(f"ADX trending bull ({adx:.0f})")
        elif ind.get("adx_bear"):
            short_score += SWING_WEIGHTS["adx"]
            short_reasons.append(f"ADX trending bear ({adx:.0f})")
        else:
            # Trending but no DI direction — partial weight
            long_score  += SWING_WEIGHTS["adx"] * 0.3
            short_score += SWING_WEIGHTS["adx"] * 0.3
    elif adx:
        # Ranging market — moderate discount
        if ind.get("ema_bull"):
            long_score  += SWING_WEIGHTS["adx"] * 0.4
        elif ind.get("ema_bear"):
            short_score += SWING_WEIGHTS["adx"] * 0.4

    # ── MACD (weight 20) ──────────────────────────────────────────────────────
    if ind.get("macd_cross_bull"):
        long_score += SWING_WEIGHTS["macd"]
        long_reasons.append("MACD bullish crossover")
    elif ind.get("macd_cross_bear"):
        short_score += SWING_WEIGHTS["macd"]
        short_reasons.append("MACD bearish crossover")
    elif ind.get("macd_hist") is not None:
        h = ind["macd_hist"]
        if h > 0:
            long_score  += SWING_WEIGHTS["macd"] * 0.6
            if ind.get("macd_line", 0) > 0:
                long_score  += SWING_WEIGHTS["macd"] * 0.2
                long_reasons.append("MACD above zero bullish")
        else:
            short_score += SWING_WEIGHTS["macd"] * 0.6
            if ind.get("macd_line", 0) < 0:
                short_score += SWING_WEIGHTS["macd"] * 0.2
                short_reasons.append("MACD below zero bearish")

    # ── RSI momentum (weight 15) ──────────────────────────────────────────────
    rsi = ind.get("rsi")
    if rsi is not None:
        if rsi < RSI_OVERSOLD:
            long_score += SWING_WEIGHTS["rsi"]
            long_reasons.append(f"RSI oversold ({rsi:.1f}) — potential reversal")
        elif rsi > RSI_OVERBOUGHT:
            short_score += SWING_WEIGHTS["rsi"]
            short_reasons.append(f"RSI overbought ({rsi:.1f}) — potential reversal")
        elif 45 < rsi < 60 and long_score > short_score:
            long_score += SWING_WEIGHTS["rsi"] * 0.5
            long_reasons.append(f"RSI bullish zone ({rsi:.1f})")
        elif 40 < rsi < 55 and short_score > long_score:
            short_score += SWING_WEIGHTS["rsi"] * 0.5

    # ── OBV volume confirmation (weight 15) ───────────────────────────────────
    if ind.get("obv_rising") is True:
        long_score += SWING_WEIGHTS["obv"]
        long_reasons.append("OBV rising — accumulation")
    elif ind.get("obv_rising") is False:
        short_score += SWING_WEIGHTS["obv"]
        short_reasons.append("OBV falling — distribution")

    # ── EMA200 bias (weight 10) ───────────────────────────────────────────────
    if ind.get("above_200") is True:
        long_score += SWING_WEIGHTS["ema200"]
        long_reasons.append("Price above EMA200 macro bull")
    elif ind.get("above_200") is False:
        short_score += SWING_WEIGHTS["ema200"]
        short_reasons.append("Price below EMA200 macro bear")

    # ── Higher TF confirmation (bonus up to +15) ──────────────────────────────
    if ind_high:
        h = ind_high
        bonus = 0
        if h.get("ema_bull"):
            bonus += 8
        if (h.get("adx") or 0) > ADX_TREND_MIN and h.get("adx_bull"):
            bonus += 7
        if bonus > 0 and long_score > short_score:
            long_score = min(long_score + bonus, 100)
            if bonus >= 8:
                long_reasons.append("Higher TF confirms trend")

        bonus = 0
        if h.get("ema_bear"):
            bonus += 8
        if (h.get("adx") or 0) > ADX_TREND_MIN and h.get("adx_bear"):
            bonus += 7
        if bonus > 0 and short_score > long_score:
            short_score = min(short_score + bonus, 100)
            if bonus >= 8:
                short_reasons.append("Higher TF confirms trend")

    # ── Determine direction ───────────────────────────────────────────────────
    direction  = None
    confidence = 0
    reasons    = []

    if long_score > short_score and long_score >= 50:
        direction  = "LONG"
        confidence = min(round(long_score), 100)
        reasons    = long_reasons
    elif short_score > long_score and short_score >= 50:
        direction  = "SHORT"
        confidence = min(round(short_score), 100)
        reasons    = short_reasons

    return {
        "direction":   direction,
        "confidence":  confidence,
        "reasons":     reasons,
        "long_score":  round(long_score, 1),
        "short_score": round(short_score, 1),
        "indicators":  ind,
    }


def _empty():
    return {"direction": None, "confidence": 0, "reasons": [], "long_score": 0, "short_score": 0, "indicators": {}}
