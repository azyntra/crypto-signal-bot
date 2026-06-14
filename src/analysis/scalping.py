"""
scalping.py — Scalping strategy for 1m / 5m / 15m timeframes.
v3.0: Market regime filter, S/R proximity, VWAP confirmation,
      plus all v2 hardening (candle filter, HTF penalty, divergence).
"""
from typing import Optional
from config.settings import (
    RSI_OVERSOLD, RSI_OVERBOUGHT, STOCH_OVERSOLD, STOCH_OVERBOUGHT,
    SR_BLOCK_PROXIMITY_PCT, SR_PENALTY_PROXIMITY_PCT,
)
from src.analysis.indicators import _sr_risk
from config.logger import get_logger

logger = get_logger(__name__)

SCALP_WEIGHTS = {
    "rsi":        25,
    "macd":       25,
    "ema":        20,
    "bb":         20,
    "stoch":      15,
    "volume":     15,
    "divergence": 15,
    "vwap":       10,
    "structure":  10,
}


def score_scalp(
    ind_fast: Optional[dict],
    ind_mid:  Optional[dict],
) -> dict:
    if not ind_fast:
        return _empty()

    ind = ind_fast
    conf = ind_mid

    # ━━ MARKET REGIME GATE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    regime = ind.get("market_regime", "unknown")
    choppy_penalty = 0.8 if regime == "choppy" else 1.0

    long_score  = 0
    short_score = 0
    long_reasons  = []
    short_reasons = []

    # ── RSI (weight 16) ──────────────────────────────────────────────────────
    if ind.get("rsi") is not None:
        rsi = ind["rsi"]
        if rsi < RSI_OVERSOLD:
            long_score += SCALP_WEIGHTS["rsi"]
            long_reasons.append(f"RSI oversold ({rsi:.1f})")
        elif rsi > RSI_OVERBOUGHT:
            short_score += SCALP_WEIGHTS["rsi"]
            short_reasons.append(f"RSI overbought ({rsi:.1f})")
        elif rsi < 40:
            long_score += SCALP_WEIGHTS["rsi"] * 0.3
        elif rsi > 60:
            short_score += SCALP_WEIGHTS["rsi"] * 0.3

    # ── MACD cross (weight 16) ────────────────────────────────────────────────
    if ind.get("macd_cross_bull"):
        long_score += SCALP_WEIGHTS["macd"]
        long_reasons.append("MACD bullish cross")
    elif ind.get("macd_cross_bear"):
        short_score += SCALP_WEIGHTS["macd"]
        short_reasons.append("MACD bearish cross")
    elif ind.get("macd_hist") is not None:
        h = ind["macd_hist"]
        if h > 0:
            long_score  += SCALP_WEIGHTS["macd"] * 0.3
        elif h < 0:
            short_score += SCALP_WEIGHTS["macd"] * 0.3

    # ── EMA alignment (weight 14) ─────────────────────────────────────────────
    if ind.get("ema_bull"):
        long_score += SCALP_WEIGHTS["ema"]
        long_reasons.append("EMA 9>21>50 bullish stack")
    elif ind.get("ema_bear"):
        short_score += SCALP_WEIGHTS["ema"]
        short_reasons.append("EMA 9<21<50 bearish stack")
    elif ind.get("price") and ind.get("ema9"):
        if ind["price"] > ind["ema9"]:
            long_score  += SCALP_WEIGHTS["ema"] * 0.3
        else:
            short_score += SCALP_WEIGHTS["ema"] * 0.3

    # ── Bollinger Band (weight 14) ────────────────────────────────────────────
    if ind.get("bb_lower") and ind.get("price") and ind.get("bb_upper"):
        price   = ind["price"]
        bb_low  = ind["bb_lower"]
        bb_high = ind["bb_upper"]
        squeeze = ind.get("bb_squeeze", False)

        if price <= bb_low:
            long_score += SCALP_WEIGHTS["bb"]
            long_reasons.append("Price at BB lower band")
        elif price >= bb_high:
            short_score += SCALP_WEIGHTS["bb"]
            short_reasons.append("Price at BB upper band")
        elif squeeze:
            if ind.get("ema_bull"):
                long_score  += SCALP_WEIGHTS["bb"] * 0.6
                long_reasons.append("BB squeeze (bull bias)")
            elif ind.get("ema_bear"):
                short_score += SCALP_WEIGHTS["bb"] * 0.6
                short_reasons.append("BB squeeze (bear bias)")

    # ── Stochastic (weight 10) ────────────────────────────────────────────────
    if ind.get("stoch_k") is not None and ind.get("stoch_d") is not None:
        sk = ind["stoch_k"]
        if ind.get("stoch_bull") or (sk < STOCH_OVERSOLD):
            long_score += SCALP_WEIGHTS["stoch"]
            long_reasons.append(f"Stochastic oversold ({sk:.1f})")
        elif ind.get("stoch_bear") or (sk > STOCH_OVERBOUGHT):
            short_score += SCALP_WEIGHTS["stoch"]
            short_reasons.append(f"Stochastic overbought ({sk:.1f})")

    # ── RSI divergence (weight 8) ─────────────────────────────────────────────
    if ind.get("rsi_bull_div"):
        long_score += SCALP_WEIGHTS["divergence"]
        long_reasons.append("RSI bullish divergence")
    if ind.get("rsi_bear_div"):
        short_score += SCALP_WEIGHTS["divergence"]
        short_reasons.append("RSI bearish divergence")

    # ── VWAP confirmation (weight 6) ──────────────────────────────────────────
    if ind.get("above_vwap") is True and long_score > short_score:
        long_score += SCALP_WEIGHTS["vwap"]
        long_reasons.append("Price above VWAP")
    elif ind.get("above_vwap") is False and short_score > long_score:
        short_score += SCALP_WEIGHTS["vwap"]
        short_reasons.append("Price below VWAP")

    # ── Price structure (weight 6) ────────────────────────────────────────────
    if ind.get("structure_bull"):
        long_score += SCALP_WEIGHTS["structure"]
        long_reasons.append("Bull price structure")
    if ind.get("structure_bear"):
        short_score += SCALP_WEIGHTS["structure"]
        short_reasons.append("Bear price structure")

    # ── Volume spike (weight 10) ──────────────────────────────────────────────
    if ind.get("vol_spike"):
        vr = ind.get("vol_ratio", 1)
        bonus = min(SCALP_WEIGHTS["volume"] * min(vr - 1, 1), SCALP_WEIGHTS["volume"])
        if long_score >= short_score:
            long_score  += bonus
            long_reasons.append(f"Volume spike ×{vr:.1f}")
        else:
            short_score += bonus
            short_reasons.append(f"Volume spike ×{vr:.1f}")

    # ── Higher-TF confirmation / penalty ──────────────────────────────────────
    if conf:
        htf_bull = conf.get("ema_bull") or (conf.get("macd_hist", 0) or 0) > 0
        htf_bear = conf.get("ema_bear") or (conf.get("macd_hist", 0) or 0) < 0

        if htf_bull and long_score > short_score:
            long_score  = min(long_score  + 8, 100)
            long_reasons.append("15m TF confirms bull")
        if htf_bear and short_score > long_score:
            short_score = min(short_score + 8, 100)
            short_reasons.append("15m TF confirms bear")

        if long_score > short_score and htf_bear and not htf_bull:
            long_score *= 0.7
        if short_score > long_score and htf_bull and not htf_bear:
            short_score *= 0.7

    # ── Determine direction ───────────────────────────────────────────────────
    direction = None
    confidence = 0
    reasons = []

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

    # ━━ S/R PROXIMITY CHECK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    price = ind.get("price", 0)
    sr_status = _sr_risk(
        price, direction,
        ind.get("nearest_resistance"), ind.get("nearest_support"),
        SR_BLOCK_PROXIMITY_PCT, SR_PENALTY_PROXIMITY_PCT,
    )
    if sr_status == "blocked":
        logger.debug(f"Scalp {direction} rejected: too close to S/R level")
        return _empty()
    elif sr_status == "close":
        confidence = int(confidence * 0.8)

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
