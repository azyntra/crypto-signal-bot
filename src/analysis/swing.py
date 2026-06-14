"""
swing.py — Swing trading strategy for 1h / 4h / 1d timeframes.
v3.0: Market regime filter, S/R proximity check, price structure weight,
      VWAP confirmation, plus all v2 hardening.
"""
from typing import Optional
from config.settings import (
    RSI_OVERSOLD, RSI_OVERBOUGHT, ADX_TREND_MIN,
    COUNTER_TREND_BLOCK, SWING_HTF_REQUIRED, ADX_SWING_MIN,
    SR_BLOCK_PROXIMITY_PCT, SR_PENALTY_PROXIMITY_PCT,
)
from src.analysis.indicators import _sr_risk
from config.logger import get_logger

logger = get_logger(__name__)

SWING_WEIGHTS = {
    "ema_trend":   30,
    "adx":         25,
    "macd":        25,
    "rsi":         20,
    "divergence":  15,
    "obv":         15,
    "ema200":      10,
    "structure":   10,   # Price structure (higher highs/lows)
    "vwap":        10,   # VWAP confirmation
}


def score_swing(
    ind_base: Optional[dict],
    ind_high: Optional[dict],
) -> dict:
    if not ind_base:
        return _empty()

    ind = ind_base

    # ━━ MARKET REGIME GATE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    regime = ind.get("market_regime", "unknown")
    choppy_penalty = 0.8 if regime == "choppy" else 1.0

    long_score  = 0.0
    short_score = 0.0
    long_reasons  = []
    short_reasons = []

    # ━━ TREND GUARD ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if COUNTER_TREND_BLOCK and ind.get("above_200") is not None:
        trend_is_bull = ind["above_200"]
    else:
        trend_is_bull = None

    # ── EMA trend alignment (weight 18) ───────────────────────────────────────
    if ind.get("ema_bull"):
        long_score += SWING_WEIGHTS["ema_trend"]
        long_reasons.append("EMA 9>21>50 bull alignment")
    elif ind.get("ema_bear"):
        short_score += SWING_WEIGHTS["ema_trend"]
        short_reasons.append("EMA 9<21<50 bear alignment")
    elif ind.get("price") and ind.get("ema50"):
        if ind["price"] > ind["ema50"]:
            long_score  += SWING_WEIGHTS["ema_trend"] * 0.3
        else:
            short_score += SWING_WEIGHTS["ema_trend"] * 0.3

    # ── ADX trend strength (weight 16) ────────────────────────────────────────
    adx = ind.get("adx")
    if adx and adx > ADX_SWING_MIN:
        if ind.get("adx_bull"):
            long_score += SWING_WEIGHTS["adx"]
            long_reasons.append(f"ADX trending bull ({adx:.0f})")
        elif ind.get("adx_bear"):
            short_score += SWING_WEIGHTS["adx"]
            short_reasons.append(f"ADX trending bear ({adx:.0f})")
        else:
            long_score  += SWING_WEIGHTS["adx"] * 0.15
            short_score += SWING_WEIGHTS["adx"] * 0.15
    elif adx and adx > ADX_TREND_MIN:
        if ind.get("ema_bull"):
            long_score  += SWING_WEIGHTS["adx"] * 0.25
        elif ind.get("ema_bear"):
            short_score += SWING_WEIGHTS["adx"] * 0.25

    # ── MACD (weight 16) ──────────────────────────────────────────────────────
    if ind.get("macd_cross_bull"):
        long_score += SWING_WEIGHTS["macd"]
        long_reasons.append("MACD bullish crossover")
    elif ind.get("macd_cross_bear"):
        short_score += SWING_WEIGHTS["macd"]
        short_reasons.append("MACD bearish crossover")
    elif ind.get("macd_hist") is not None:
        h = ind["macd_hist"]
        if h > 0:
            long_score  += SWING_WEIGHTS["macd"] * 0.3
        else:
            short_score += SWING_WEIGHTS["macd"] * 0.3

    # ── RSI momentum (weight 10) — confirms, not reversal ────────────────────
    rsi = ind.get("rsi")
    if rsi is not None:
        if regime == "trending":
            # In trending: RSI confirms momentum direction
            if 50 < rsi < RSI_OVERBOUGHT and long_score > short_score:
                long_score += SWING_WEIGHTS["rsi"]
                long_reasons.append(f"RSI bullish momentum ({rsi:.1f})")
            elif 30 < rsi < 50 and short_score > long_score:
                short_score += SWING_WEIGHTS["rsi"]
                short_reasons.append(f"RSI bearish momentum ({rsi:.1f})")
        elif regime == "ranging":
            # In ranging: RSI extremes are valid reversal signals
            if rsi < RSI_OVERSOLD:
                long_score += SWING_WEIGHTS["rsi"]
                long_reasons.append(f"RSI oversold reversal ({rsi:.1f})")
            elif rsi > RSI_OVERBOUGHT:
                short_score += SWING_WEIGHTS["rsi"]
                short_reasons.append(f"RSI overbought reversal ({rsi:.1f})")
        else:
            # Unknown regime — use partial scoring
            if rsi > RSI_OVERBOUGHT:
                short_score += SWING_WEIGHTS["rsi"] * 0.5
            elif rsi < RSI_OVERSOLD:
                long_score  += SWING_WEIGHTS["rsi"] * 0.5

    # ── RSI divergence (weight 10) ────────────────────────────────────────────
    if ind.get("rsi_bull_div"):
        long_score += SWING_WEIGHTS["divergence"]
        long_reasons.append("RSI bullish divergence detected")
    if ind.get("rsi_bear_div"):
        short_score += SWING_WEIGHTS["divergence"]
        short_reasons.append("RSI bearish divergence detected")

    # ── OBV volume confirmation (weight 8) ────────────────────────────────────
    if ind.get("obv_rising") is True:
        long_score += SWING_WEIGHTS["obv"]
        long_reasons.append("OBV rising — accumulation")
    elif ind.get("obv_rising") is False:
        short_score += SWING_WEIGHTS["obv"]
        short_reasons.append("OBV falling — distribution")

    # ── EMA200 bias (weight 8) ────────────────────────────────────────────────
    if ind.get("above_200") is True:
        long_score += SWING_WEIGHTS["ema200"]
        long_reasons.append("Price above EMA200 macro bull")
    elif ind.get("above_200") is False:
        short_score += SWING_WEIGHTS["ema200"]
        short_reasons.append("Price below EMA200 macro bear")

    # ── Price structure (weight 8) ────────────────────────────────────────────
    if ind.get("structure_bull"):
        long_score += SWING_WEIGHTS["structure"]
        long_reasons.append("Higher highs + higher lows (bull structure)")
    if ind.get("structure_bear"):
        short_score += SWING_WEIGHTS["structure"]
        short_reasons.append("Lower highs + lower lows (bear structure)")

    # ── VWAP confirmation (weight 6) ──────────────────────────────────────────
    if ind.get("above_vwap") is True and long_score > short_score:
        long_score += SWING_WEIGHTS["vwap"]
        long_reasons.append("Price above VWAP")
    elif ind.get("above_vwap") is False and short_score > long_score:
        short_score += SWING_WEIGHTS["vwap"]
        short_reasons.append("Price below VWAP")

    # ━━ REGIME-BASED SIGNAL TYPE CHECK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if regime == "trending":
        # In trending market: only allow trend-following (EMA alignment must match)
        if long_score > short_score and not ind.get("ema_bull"):
            long_score *= 0.5  # heavy penalty for non-aligned trend signal
        if short_score > long_score and not ind.get("ema_bear"):
            short_score *= 0.5
    elif regime == "ranging":
        # In ranging market: only allow if RSI extreme or divergence present
        has_reversal_signal = (
            ind.get("rsi_bull_div") or ind.get("rsi_bear_div") or
            (rsi is not None and (rsi < RSI_OVERSOLD or rsi > RSI_OVERBOUGHT))
        )
        if not has_reversal_signal:
            logger.debug("Swing rejected: ranging market without reversal signal")
            return _empty()

    # ━━ TREND GUARD APPLICATION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if trend_is_bull is True:
        short_score = 0
        short_reasons = []
    elif trend_is_bull is False:
        long_score = 0
        long_reasons = []

    # ── Higher TF confirmation ────────────────────────────────────────────────
    htf_confirms_long  = False
    htf_confirms_short = False

    if ind_high:
        h = ind_high
        if h.get("ema_bull") or (h.get("adx_bull") and (h.get("adx") or 0) > ADX_TREND_MIN):
            htf_confirms_long = True
        if h.get("ema_bear") or (h.get("adx_bear") and (h.get("adx") or 0) > ADX_TREND_MIN):
            htf_confirms_short = True

        if htf_confirms_long and long_score > short_score:
            long_score = min(long_score + 12, 100)
            long_reasons.append("Higher TF confirms bull trend")
        if htf_confirms_short and short_score > long_score:
            short_score = min(short_score + 12, 100)
            short_reasons.append("Higher TF confirms bear trend")

        if long_score > short_score and htf_confirms_short and not htf_confirms_long:
            long_score *= 0.6
        if short_score > long_score and htf_confirms_long and not htf_confirms_short:
            short_score *= 0.6

    if SWING_HTF_REQUIRED and ind_high:
        if long_score > short_score and not htf_confirms_long:
            logger.debug("Swing long rejected: no higher-TF confirmation")
            return _empty()
        if short_score > long_score and not htf_confirms_short:
            logger.debug("Swing short rejected: no higher-TF confirmation")
            return _empty()

    # ── Determine direction ───────────────────────────────────────────────────
    direction  = None
    confidence = 0
    reasons    = []

    long_score *= choppy_penalty
    short_score *= choppy_penalty

    if long_score > short_score and long_score >= 50:
        direction  = "LONG"
        confidence = min(round(long_score), 100)
        reasons    = long_reasons
    elif short_score > long_score and short_score >= 50:
        direction  = "SHORT"
        confidence = min(round(short_score), 100)
        reasons    = short_reasons

    if not direction:
        return _empty()

    # ━━ S/R PROXIMITY CHECK (post-scoring) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    price = ind.get("price", 0)
    sr_status = _sr_risk(
        price, direction,
        ind.get("nearest_resistance"), ind.get("nearest_support"),
        SR_BLOCK_PROXIMITY_PCT, SR_PENALTY_PROXIMITY_PCT,
    )
    if sr_status == "blocked":
        logger.debug(f"Swing {direction} rejected: too close to S/R level")
        return _empty()
    elif sr_status == "close":
        confidence = int(confidence * 0.8)  # 20% penalty
        reasons.append("⚠ Near S/R level — confidence reduced")

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
