"""
strategies.py — Regime-gated signal strategies (v3).

Design philosophy (this is what fixes the win rate):
  v2 mixed mean-reversion and trend triggers into one additive score, so a
  weak drift market could reach the threshold from scraps of unrelated
  points. v3 uses HARD GATES: each strategy only fires in the regime it is
  designed for, and every gate must pass. Confidence is then scored from
  optional confluence on top — it can only rank signals, never rescue a
  setup that failed a gate.

Strategies:
  A. trend_pullback : in an established trend, buy the pullback to the
                      EMA21/VWAP zone after a resumption trigger candle.
  B. range_fade     : in a quiet range, fade the extremes at BB band + S/R
                      with an oversold/overbought oscillator and a reversal
                      candle.
  C. squeeze_breakout: volatility squeeze breaking out with volume, in the
                      direction of the higher-timeframe bias.

Each returns None or a candidate dict:
  {direction, strategy, confidence, reasons[], indicators, sl_basis}
"""
from typing import Optional

from config.logger import get_logger
from config.settings import ADX_TREND_MIN, BBW_PCTILE_SQUEEZE

logger = get_logger(__name__)


def _base(direction, strategy, reasons, ind, sl_basis):
    return {
        "direction": direction,
        "strategy": strategy,
        "confidence": 0,
        "reasons": reasons,
        "indicators": ind,
        "sl_basis": sl_basis,
    }


# ── A. Trend pullback ─────────────────────────────────────────────────────────

def trend_pullback(ind: dict, ind_htf: dict, regime: str) -> Optional[dict]:
    """
    ind     : entry timeframe indicators (15m intraday / 1h swing)
    ind_htf : higher timeframe indicators (1h / 4h)
    regime  : per-coin 4h regime
    """
    if regime not in ("trend_up", "trend_down"):
        return None
    price, atr = ind.get("price"), ind.get("atr")
    if not price or not atr:
        return None

    if regime == "trend_up":
        # HARD GATES
        if ind.get("ema21") is None or price < ind["ema21"] * 0.995:
            return None                                    # trend intact on entry TF
        if not ind.get("touched_ema21"):
            return None                                    # there WAS a pullback
        rsi = ind.get("rsi")
        if rsi is None or not (38 <= rsi <= 62):
            return None                                    # healthy reset, not a crash
        if not ind.get("candle_bull"):
            return None                                    # resumption trigger candle
        if ind.get("supertrend_dir") == -1:
            return None
        # Room to move: next resistance at least 1.5 ATR away
        res = ind.get("nearest_resistance")
        if res and (res - price) < 1.5 * atr:
            return None

        reasons = ["Uptrend pullback to EMA21 zone", "Bullish resumption candle"]
        sl_basis = ind.get("last_swing_low")
        return _score_trend(_base("LONG", "trend_pullback", reasons, ind, sl_basis), ind, ind_htf, bull=True)

    else:  # trend_down
        if ind.get("ema21") is None or price > ind["ema21"] * 1.005:
            return None
        if not ind.get("touched_ema21"):
            return None
        rsi = ind.get("rsi")
        if rsi is None or not (38 <= rsi <= 62):
            return None
        if ind.get("candle_bull"):
            return None
        if ind.get("supertrend_dir") == 1:
            return None
        sup = ind.get("nearest_support")
        if sup and (price - sup) < 1.5 * atr:
            return None

        reasons = ["Downtrend pullback to EMA21 zone", "Bearish resumption candle"]
        sl_basis = ind.get("last_swing_high")
        return _score_trend(_base("SHORT", "trend_pullback", reasons, ind, sl_basis), ind, ind_htf, bull=False)


def _score_trend(cand: dict, ind: dict, htf: Optional[dict], bull: bool) -> dict:
    score = 55
    r = cand["reasons"]

    adx = ind.get("adx") or 0
    if adx >= 30:
        score += 8; r.append(f"Strong ADX {adx:.0f}")
    elif adx >= ADX_TREND_MIN:
        score += 4

    if bull and ind.get("structure_bull") or (not bull and ind.get("structure_bear")):
        score += 6; r.append("Price structure confirms (HH/HL)" if bull else "Price structure confirms (LH/LL)")

    if bull and ind.get("macd_hist_rising") or (not bull and ind.get("macd_hist") is not None and not ind.get("macd_hist_rising")):
        score += 4

    if bull and ind.get("bull_engulf") or (not bull and ind.get("bear_engulf")):
        score += 6; r.append("Engulfing trigger candle")
    elif bull and ind.get("bull_pin") or (not bull and ind.get("bear_pin")):
        score += 5; r.append("Pin bar at pullback zone")

    if (ind.get("vol_ratio") or 1) >= 1.2:
        score += 4; r.append(f"Volume {ind['vol_ratio']:.1f}x average")

    cmf = ind.get("cmf")
    if cmf is not None and ((bull and cmf > 0.05) or (not bull and cmf < -0.05)):
        score += 4; r.append("Money flow confirms")

    if ind.get("obv_rising") is bull:
        score += 3

    if htf:
        if (bull and htf.get("ema_bull")) or (not bull and htf.get("ema_bear")):
            score += 6; r.append("Higher TF trend aligned")
        if (bull and htf.get("supertrend_dir") == 1) or (not bull and htf.get("supertrend_dir") == -1):
            score += 4

    vwap_ok = ind.get("above_vwap")
    if vwap_ok is bull:
        score += 3

    cand["confidence"] = min(score, 95)
    return cand


# ── B. Range fade ─────────────────────────────────────────────────────────────

def range_fade(ind: dict, ind_htf: dict, regime: str) -> Optional[dict]:
    if regime != "range":
        return None
    price, atr = ind.get("price"), ind.get("atr")
    bb_low, bb_up, bb_mid = ind.get("bb_lower"), ind.get("bb_upper"), ind.get("bb_mid")
    if not all([price, atr, bb_low, bb_up, bb_mid]):
        return None

    rsi, mfi = ind.get("rsi"), ind.get("mfi")
    sup, res = ind.get("nearest_support"), ind.get("nearest_resistance")

    # LONG at the bottom of the range
    if price <= bb_low * 1.002:
        if rsi is None or rsi > 32:
            return None
        if mfi is not None and mfi > 30:
            return None
        if not (ind.get("bull_pin") or ind.get("bull_engulf") or ind.get("candle_bull")):
            return None                                      # need a reversal candle
        if ind.get("vol_spike") and not ind.get("candle_bull"):
            return None                                      # heavy selling — don't catch the knife
        if sup is None or (price - sup) > 1.0 * atr:
            sup_ok = sup is not None and abs(price - sup) <= 1.0 * atr
            if not sup_ok:
                return None                                  # must be AT support
        reasons = [f"Range low fade: RSI {rsi:.0f} at BB lower + support"]
        cand = _base("LONG", "range_fade", reasons, ind, sup)
        return _score_range(cand, ind, bull=True)

    # SHORT at the top of the range
    if price >= bb_up * 0.998:
        if rsi is None or rsi < 68:
            return None
        if mfi is not None and mfi < 70:
            return None
        if not (ind.get("bear_pin") or ind.get("bear_engulf") or not ind.get("candle_bull")):
            return None
        if ind.get("vol_spike") and ind.get("candle_bull"):
            return None                                      # breakout in progress — don't fade
        if res is None or (res - price) > 1.0 * atr:
            return None
        reasons = [f"Range high fade: RSI {rsi:.0f} at BB upper + resistance"]
        cand = _base("SHORT", "range_fade", reasons, ind, res)
        return _score_range(cand, ind, bull=False)

    return None


def _score_range(cand: dict, ind: dict, bull: bool) -> dict:
    score = 55
    r = cand["reasons"]
    rsi = ind.get("rsi") or 50
    if (bull and rsi < 25) or (not bull and rsi > 75):
        score += 6; r.append("Deep oscillator extreme")
    if (bull and ind.get("bull_pin")) or (not bull and ind.get("bear_pin")):
        score += 7; r.append("Pin bar rejection")
    elif (bull and ind.get("bull_engulf")) or (not bull and ind.get("bear_engulf")):
        score += 7; r.append("Engulfing reversal")
    mfi = ind.get("mfi")
    if mfi is not None and ((bull and mfi < 15) or (not bull and mfi > 85)):
        score += 4
    stoch = ind.get("stoch_k")
    if stoch is not None and ((bull and stoch < 15) or (not bull and stoch > 85)):
        score += 4; r.append("Stochastic extreme")
    if (ind.get("bbw_pctile") or 100) < 40:
        score += 4                                           # a tight, well-behaved range
    cand["confidence"] = min(score, 90)                      # fades cap lower than trends
    return cand


# ── C. Squeeze breakout ───────────────────────────────────────────────────────

def squeeze_breakout(ind: dict, ind_htf: dict, regime: str) -> Optional[dict]:
    if regime == "choppy":
        return None
    price, atr = ind.get("price"), ind.get("atr")
    if not price or not atr:
        return None

    bbw_pct = ind.get("bbw_pctile")
    if bbw_pct is None or bbw_pct > BBW_PCTILE_SQUEEZE + 10:
        return None                                          # must come out of a squeeze

    vol_ratio = ind.get("vol_ratio") or 1
    if vol_ratio < 1.8:
        return None                                          # breakout needs volume
    if (ind.get("body_pct") or 0) < 0.55:
        return None                                          # decisive candle

    don_high, don_low = ind.get("donchian_high"), ind.get("donchian_low")

    # Bullish breakout
    if don_high and price > don_high and ind.get("candle_bull"):
        if regime == "trend_down":
            return None                                      # never long a breakout in a downtrend
        if ind_htf and ind_htf.get("supertrend_dir") == -1 and not ind_htf.get("above_200"):
            return None
        reasons = [f"Squeeze breakout above {don_high:.6g} on {vol_ratio:.1f}x volume"]
        cand = _base("LONG", "squeeze_breakout", reasons, ind, don_low)
        return _score_breakout(cand, ind, ind_htf, bull=True)

    # Bearish breakdown
    if don_low and price < don_low and not ind.get("candle_bull"):
        if regime == "trend_up":
            return None
        if ind_htf and ind_htf.get("supertrend_dir") == 1 and ind_htf.get("above_200"):
            return None
        reasons = [f"Squeeze breakdown below {don_low:.6g} on {vol_ratio:.1f}x volume"]
        cand = _base("SHORT", "squeeze_breakout", reasons, ind, don_high)
        return _score_breakout(cand, ind, ind_htf, bull=False)

    return None


def _score_breakout(cand: dict, ind: dict, htf: Optional[dict], bull: bool) -> dict:
    score = 55
    r = cand["reasons"]
    if (ind.get("vol_ratio") or 1) >= 2.5:
        score += 7; r.append("Exceptional breakout volume")
    if (bull and ind.get("st_flip_bull")) or (not bull and ind.get("st_flip_bear")):
        score += 6; r.append("SuperTrend flip confirms")
    if (bull and ind.get("macd_cross_bull")) or (not bull and ind.get("macd_cross_bear")):
        score += 5; r.append("MACD cross confirms")
    cmf = ind.get("cmf")
    if cmf is not None and ((bull and cmf > 0.1) or (not bull and cmf < -0.1)):
        score += 5; r.append("Strong money flow")
    if htf and ((bull and htf.get("ema_bull")) or (not bull and htf.get("ema_bear"))):
        score += 6; r.append("Higher TF aligned")
    if (ind.get("bbw_pctile") or 100) <= BBW_PCTILE_SQUEEZE:
        score += 4; r.append("Deep volatility squeeze")
    cand["confidence"] = min(score, 95)
    return cand


# ── Entry point ───────────────────────────────────────────────────────────────

STRATEGIES = [trend_pullback, range_fade, squeeze_breakout]


def evaluate(ind_entry: dict, ind_htf: dict, regime: str) -> Optional[dict]:
    """Run all strategies; return the highest-confidence candidate."""
    best = None
    for strat in STRATEGIES:
        try:
            cand = strat(ind_entry, ind_htf, regime)
            if cand and (best is None or cand["confidence"] > best["confidence"]):
                best = cand
        except Exception as e:
            logger.debug(f"Strategy {strat.__name__} error: {e}")
    return best
