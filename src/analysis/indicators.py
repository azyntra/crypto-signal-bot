"""
indicators.py — Computes all technical indicators on a price DataFrame.
v2.0: Added market regime detection, support/resistance levels,
      price structure analysis, VWAP, and RSI divergence.

Uses the `ta` library (pure-Python, arm64-compatible, no TA-Lib compile needed).
Returns a flat dict of indicator values for the scorer to consume.
"""
import pandas as pd
import numpy as np
from typing import Optional

import ta.momentum as tam
import ta.trend    as tat
import ta.volatility as tav
import ta.volume   as tavo

from config.logger import get_logger
from config.settings import (
    RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    BB_PERIOD, BB_STD, EMA_FAST, EMA_MID, EMA_SLOW, EMA_TREND,
    ADX_PERIOD, ATR_PERIOD, STOCH_K, STOCH_D, OBV_MA_PERIOD,
    REGIME_TRENDING_ADX, REGIME_RANGING_ADX,
    REGIME_TRENDING_BBW, REGIME_RANGING_BBW,
    SR_LOOKBACK_CANDLES,
)

logger = get_logger(__name__)


def _safe(series, idx=-1):
    """Safely extract a value from a pandas Series."""
    try:
        v = series.iloc[idx]
        return None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
    except Exception:
        return None


# ── Support / Resistance detection ────────────────────────────────────────────

def _detect_swing_points(high: pd.Series, low: pd.Series, lookback: int = 5):
    """
    Detect swing highs and swing lows using a simple pivot method.
    A swing high: high[i] is higher than the N bars before and after it.
    A swing low: low[i] is lower than the N bars before and after it.
    """
    swing_highs = []
    swing_lows = []
    n = len(high)
    pivot = max(2, lookback // 4)  # smaller pivot for more points

    for i in range(pivot, n - pivot):
        # Swing high
        if all(high.iloc[i] >= high.iloc[i - j] for j in range(1, pivot + 1)) and \
           all(high.iloc[i] >= high.iloc[i + j] for j in range(1, pivot + 1)):
            swing_highs.append(float(high.iloc[i]))
        # Swing low
        if all(low.iloc[i] <= low.iloc[i - j] for j in range(1, pivot + 1)) and \
           all(low.iloc[i] <= low.iloc[i + j] for j in range(1, pivot + 1)):
            swing_lows.append(float(low.iloc[i]))

    return swing_highs, swing_lows


def _find_nearest_sr(price: float, swing_highs: list, swing_lows: list):
    """Find nearest resistance (above) and support (below) from swing points."""
    resistances = sorted([h for h in swing_highs if h > price])
    supports = sorted([l for l in swing_lows if l < price], reverse=True)

    nearest_resistance = resistances[0] if resistances else None
    nearest_support = supports[0] if supports else None

    return nearest_resistance, nearest_support


def _sr_risk(price: float, direction: str, nearest_resistance, nearest_support,
             block_pct: float = 1.0, penalty_pct: float = 2.0) -> str:
    """
    Check if signal direction faces a nearby S/R wall.
    LONG near resistance → risky. SHORT near support → risky.
    Returns: "clear", "close", or "blocked"
    """
    if direction == "LONG" and nearest_resistance:
        dist_pct = (nearest_resistance - price) / price * 100
        if dist_pct < block_pct:
            return "blocked"
        elif dist_pct < penalty_pct:
            return "close"
    elif direction == "SHORT" and nearest_support:
        dist_pct = (price - nearest_support) / price * 100
        if dist_pct < block_pct:
            return "blocked"
        elif dist_pct < penalty_pct:
            return "close"
    return "clear"


# ── Price structure (higher highs / lower lows) ──────────────────────────────

def _detect_price_structure(swing_highs: list, swing_lows: list):
    """
    Detect if price is making higher highs + higher lows (bull structure)
    or lower highs + lower lows (bear structure).
    Requires at least 3 swing points in each direction.
    """
    structure_bull = False
    structure_bear = False

    if len(swing_highs) >= 3 and len(swing_lows) >= 3:
        # Check last 3 swing highs ascending
        last_highs = swing_highs[-3:]
        highs_ascending = last_highs[0] < last_highs[1] < last_highs[2]

        # Check last 3 swing lows ascending
        last_lows = swing_lows[-3:]
        lows_ascending = last_lows[0] < last_lows[1] < last_lows[2]

        # Check last 3 swing highs descending
        highs_descending = last_highs[0] > last_highs[1] > last_highs[2]
        lows_descending = last_lows[0] > last_lows[1] > last_lows[2]

        structure_bull = highs_ascending and lows_ascending
        structure_bear = highs_descending and lows_descending

    return structure_bull, structure_bear


# ── Market regime classification ──────────────────────────────────────────────

def _classify_regime(adx, bb_width, ema_bull, ema_bear) -> str:
    """
    Classify market into: "trending", "ranging", or "choppy".
    - Trending: ADX > 25 AND (BB expanding OR EMAs aligned)
    - Ranging:  ADX < 20 AND BB narrow
    - Choppy:   everything else (no clear state)
    """
    if adx is None or bb_width is None:
        return "unknown"

    if adx > REGIME_TRENDING_ADX and (bb_width > REGIME_TRENDING_BBW or ema_bull or ema_bear):
        return "trending"
    elif adx < REGIME_RANGING_ADX and bb_width < REGIME_RANGING_BBW:
        return "ranging"
    elif adx < REGIME_RANGING_ADX:
        return "ranging"  # low ADX even with wider bands = still rangy
    else:
        return "choppy"


# ── VWAP calculation ─────────────────────────────────────────────────────────

def _compute_vwap(high: pd.Series, low: pd.Series, close: pd.Series,
                  volume: pd.Series) -> Optional[float]:
    """Compute session VWAP (Volume-Weighted Average Price)."""
    try:
        typical_price = (high + low + close) / 3
        cum_vol = volume.cumsum()
        cum_tp_vol = (typical_price * volume).cumsum()
        vwap_series = cum_tp_vol / cum_vol
        val = float(vwap_series.iloc[-1])
        return val if not np.isnan(val) else None
    except Exception:
        return None


# ── RSI Divergence ───────────────────────────────────────────────────────────

def _detect_rsi_divergence(close: pd.Series, rsi_series: pd.Series, lookback: int = 14):
    """
    Detect RSI bullish and bearish divergences.
    Bullish: price lower low, RSI higher low (in oversold territory)
    Bearish: price higher high, RSI lower high (in overbought territory)
    """
    bull_div = False
    bear_div = False
    div_lookback = min(lookback, len(close) - 2)

    if div_lookback < 5:
        return bull_div, bear_div

    try:
        recent_close = close.iloc[-div_lookback:]
        recent_rsi = rsi_series.iloc[-div_lookback:]

        # Bullish divergence: price lower low, RSI higher low
        price_min_idx = recent_close.idxmin()
        if pd.notna(price_min_idx):
            price_min_pos = recent_close.index.get_loc(price_min_idx)
            if price_min_pos > 2:
                prev_low_price = recent_close.iloc[:price_min_pos].min()
                if pd.notna(prev_low_price) and float(recent_close.iloc[price_min_pos]) < prev_low_price:
                    rsi_at_new_low = float(recent_rsi.iloc[price_min_pos])
                    rsi_at_prev_low = float(recent_rsi.iloc[:price_min_pos].min())
                    if pd.notna(rsi_at_prev_low) and rsi_at_new_low > rsi_at_prev_low and rsi_at_new_low < 40:
                        bull_div = True

        # Bearish divergence: price higher high, RSI lower high
        price_max_idx = recent_close.idxmax()
        if pd.notna(price_max_idx):
            price_max_pos = recent_close.index.get_loc(price_max_idx)
            if price_max_pos > 2:
                prev_high_price = recent_close.iloc[:price_max_pos].max()
                if pd.notna(prev_high_price) and float(recent_close.iloc[price_max_pos]) > prev_high_price:
                    rsi_at_new_high = float(recent_rsi.iloc[price_max_pos])
                    rsi_at_prev_high = float(recent_rsi.iloc[:price_max_pos].max())
                    if pd.notna(rsi_at_prev_high) and rsi_at_new_high < rsi_at_prev_high and rsi_at_new_high > 60:
                        bear_div = True
    except Exception:
        pass  # divergence detection is best-effort

    return bull_div, bear_div


# ══════════════════════════════════════════════════════════════════════════════
# Main indicator computation
# ══════════════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame) -> Optional[dict]:
    """
    Given an OHLCV DataFrame, compute all indicators and return a flat dict.
    Returns None if there is not enough data.
    """
    if df is None or len(df) < 60:
        return None

    try:
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]
        open_  = df["open"]
        price  = float(close.iloc[-1])

        # ── RSI ──────────────────────────────────────────────────────────────
        rsi_indicator = tam.RSIIndicator(close, window=RSI_PERIOD)
        rsi_series = rsi_indicator.rsi()
        rsi = _safe(rsi_series)

        # ── MACD ─────────────────────────────────────────────────────────────
        macd_obj    = tat.MACD(close, window_fast=MACD_FAST, window_slow=MACD_SLOW, window_sign=MACD_SIGNAL)
        macd_line   = _safe(macd_obj.macd())
        macd_signal = _safe(macd_obj.macd_signal())
        macd_hist   = _safe(macd_obj.macd_diff())
        macd_prev   = _safe(macd_obj.macd(), -2)
        macd_sig_p  = _safe(macd_obj.macd_signal(), -2)

        macd_cross_bull = (
            macd_line is not None and macd_signal is not None and
            macd_prev is not None and macd_sig_p is not None and
            macd_line > macd_signal and macd_prev < macd_sig_p
        )
        macd_cross_bear = (
            macd_line is not None and macd_signal is not None and
            macd_prev is not None and macd_sig_p is not None and
            macd_line < macd_signal and macd_prev > macd_sig_p
        )

        # ── Bollinger Bands ───────────────────────────────────────────────────
        bb_obj   = tav.BollingerBands(close, window=BB_PERIOD, window_dev=BB_STD)
        bb_upper = _safe(bb_obj.bollinger_hband())
        bb_mid   = _safe(bb_obj.bollinger_mavg())
        bb_lower = _safe(bb_obj.bollinger_lband())
        bb_pct   = _safe(bb_obj.bollinger_pband())
        bb_wband = _safe(bb_obj.bollinger_wband())
        bb_squeeze = (bb_wband < 0.05) if bb_wband is not None else False

        # ── EMAs ─────────────────────────────────────────────────────────────
        ema9   = _safe(tat.EMAIndicator(close, window=EMA_FAST).ema_indicator())
        ema21  = _safe(tat.EMAIndicator(close, window=EMA_MID).ema_indicator())
        ema50  = _safe(tat.EMAIndicator(close, window=EMA_SLOW).ema_indicator())
        ema200 = _safe(tat.EMAIndicator(close, window=EMA_TREND).ema_indicator()) if len(df) >= 200 else None

        ema_bull = (ema9 is not None and ema21 is not None and ema50 is not None
                    and ema9 > ema21 > ema50)
        ema_bear = (ema9 is not None and ema21 is not None and ema50 is not None
                    and ema9 < ema21 < ema50)
        above_200 = (price > ema200) if ema200 is not None else None

        # ── ADX / DI ─────────────────────────────────────────────────────────
        adx_obj = tat.ADXIndicator(high, low, close, window=ADX_PERIOD)
        adx     = _safe(adx_obj.adx())
        di_pos  = _safe(adx_obj.adx_pos())
        di_neg  = _safe(adx_obj.adx_neg())
        trending  = (adx > 20) if adx is not None else False
        adx_bull  = (adx is not None and di_pos is not None and di_neg is not None
                     and adx > 20 and di_pos > di_neg)
        adx_bear  = (adx is not None and di_pos is not None and di_neg is not None
                     and adx > 20 and di_neg > di_pos)

        # ── ATR ──────────────────────────────────────────────────────────────
        atr     = _safe(tav.AverageTrueRange(high, low, close, window=ATR_PERIOD).average_true_range())
        atr_pct = (atr / price * 100) if (atr and price) else None

        # ── Stochastic ───────────────────────────────────────────────────────
        stoch_obj = tam.StochasticOscillator(high, low, close, window=STOCH_K, smooth_window=STOCH_D)
        stoch_k   = _safe(stoch_obj.stoch())
        stoch_d   = _safe(stoch_obj.stoch_signal())
        stoch_bull = (stoch_k is not None and stoch_d is not None
                      and stoch_k > stoch_d and stoch_k < 30)
        stoch_bear = (stoch_k is not None and stoch_d is not None
                      and stoch_k < stoch_d and stoch_k > 70)

        # ── OBV ──────────────────────────────────────────────────────────────
        obv_series = tavo.OnBalanceVolumeIndicator(close, volume).on_balance_volume()
        obv        = _safe(obv_series)
        obv_ma     = _safe(obv_series.rolling(OBV_MA_PERIOD).mean())
        obv_rising = (obv > obv_ma) if (obv is not None and obv_ma is not None) else None

        # ── Volume analysis ───────────────────────────────────────────────────
        vol_ma20    = float(volume.rolling(20).mean().iloc[-1])
        vol_current = float(volume.iloc[-1])
        vol_ratio   = vol_current / vol_ma20 if vol_ma20 else 1.0
        vol_spike   = vol_ratio > 1.5

        # ── Candle ───────────────────────────────────────────────────────────
        prev_close   = float(close.iloc[-2])
        body         = abs(price - float(open_.iloc[-1]))
        candle_range = float(high.iloc[-1] - low.iloc[-1])
        body_pct     = body / candle_range if candle_range else 0

        # ── RSI divergence ───────────────────────────────────────────────────
        rsi_bull_div, rsi_bear_div = _detect_rsi_divergence(close, rsi_series)

        # ── VWAP ─────────────────────────────────────────────────────────────
        vwap = _compute_vwap(high, low, close, volume)
        above_vwap = (price > vwap) if vwap is not None else None

        # ── Market regime ────────────────────────────────────────────────────
        market_regime = _classify_regime(adx, bb_wband, ema_bull, ema_bear)

        # ── Support / Resistance ─────────────────────────────────────────────
        sr_lookback = min(SR_LOOKBACK_CANDLES, len(df) - 5)
        if sr_lookback >= 10:
            sr_high = high.iloc[-sr_lookback:]
            sr_low = low.iloc[-sr_lookback:]
            swing_highs, swing_lows = _detect_swing_points(sr_high, sr_low)
        else:
            swing_highs, swing_lows = [], []

        nearest_resistance, nearest_support = _find_nearest_sr(
            price, swing_highs, swing_lows
        )

        # ── Price structure (higher highs / lower lows) ──────────────────────
        structure_bull, structure_bear = _detect_price_structure(
            swing_highs, swing_lows
        )

        return {
            "price":          price,
            "prev_close":     prev_close,
            "change_pct":     (price - prev_close) / prev_close * 100,

            "rsi":            rsi,
            "rsi_bull_div":   rsi_bull_div,
            "rsi_bear_div":   rsi_bear_div,

            "macd_line":      macd_line,
            "macd_signal":    macd_signal,
            "macd_hist":      macd_hist,
            "macd_cross_bull": macd_cross_bull,
            "macd_cross_bear": macd_cross_bear,

            "bb_upper":       bb_upper,
            "bb_mid":         bb_mid,
            "bb_lower":       bb_lower,
            "bb_pct":         bb_pct,
            "bb_width":       bb_wband,
            "bb_squeeze":     bb_squeeze,

            "ema9":           ema9,
            "ema21":          ema21,
            "ema50":          ema50,
            "ema200":         ema200,
            "ema_bull":       ema_bull,
            "ema_bear":       ema_bear,
            "above_200":      above_200,

            "adx":            adx,
            "di_pos":         di_pos,
            "di_neg":         di_neg,
            "trending":       trending,
            "adx_bull":       adx_bull,
            "adx_bear":       adx_bear,

            "atr":            atr,
            "atr_pct":        atr_pct,

            "stoch_k":        stoch_k,
            "stoch_d":        stoch_d,
            "stoch_bull":     stoch_bull,
            "stoch_bear":     stoch_bear,

            "obv":            obv,
            "obv_ma":         obv_ma,
            "obv_rising":     obv_rising,

            "vol_ratio":      vol_ratio,
            "vol_spike":      vol_spike,
            "body_pct":       body_pct,

            # Phase 2 additions
            "vwap":               vwap,
            "above_vwap":         above_vwap,
            "market_regime":      market_regime,
            "nearest_resistance": nearest_resistance,
            "nearest_support":    nearest_support,
            "structure_bull":     structure_bull,
            "structure_bear":     structure_bear,
        }

    except Exception as e:
        logger.error(f"Indicator computation error: {e}")
        return None
