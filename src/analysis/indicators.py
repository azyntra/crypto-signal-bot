"""
indicators.py — Technical indicator computation (v3).

v3 additions over v2:
  - SuperTrend, Donchian channels, MFI, CMF (Chaikin Money Flow)
  - Session-anchored VWAP (daily anchor — v2's "VWAP" was a 200-candle
    cumulative average, which is not VWAP)
  - BB-width percentile + ATR percentile (for squeeze / volatility context)
  - EMA200 slope
  - Candle patterns: bullish/bearish engulfing, pin bars
  - All computed on CLOSED candles only (last row is dropped if requested)
    so live signals never trigger off a half-formed candle.
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
    MFI_PERIOD, CMF_PERIOD, SUPERTREND_PERIOD, SUPERTREND_MULT,
    DONCHIAN_PERIOD, SR_LOOKBACK_CANDLES,
)

logger = get_logger(__name__)


def _safe(series, idx=-1):
    try:
        v = series.iloc[idx]
        return None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
    except Exception:
        return None


def _pctile_of_last(series: pd.Series, window: int = 100) -> Optional[float]:
    """Percentile rank (0-100) of the last value within its recent window."""
    try:
        s = series.dropna().iloc[-window:]
        if len(s) < 20:
            return None
        return float((s < s.iloc[-1]).mean() * 100)
    except Exception:
        return None


# ── SuperTrend ────────────────────────────────────────────────────────────────

def _supertrend(high, low, close, period=SUPERTREND_PERIOD, mult=SUPERTREND_MULT):
    """Returns (direction_series, line_series). direction: 1 bull, -1 bear."""
    atr = tav.AverageTrueRange(high, low, close, window=period).average_true_range()
    hl2 = (high + low) / 2
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr

    n = len(close)
    st_dir = np.ones(n)
    st_line = np.full(n, np.nan)
    fu = upper.copy()
    fl = lower.copy()

    for i in range(1, n):
        fu.iloc[i] = upper.iloc[i] if (upper.iloc[i] < fu.iloc[i-1] or close.iloc[i-1] > fu.iloc[i-1]) else fu.iloc[i-1]
        fl.iloc[i] = lower.iloc[i] if (lower.iloc[i] > fl.iloc[i-1] or close.iloc[i-1] < fl.iloc[i-1]) else fl.iloc[i-1]
        if st_dir[i-1] == 1:
            st_dir[i] = -1 if close.iloc[i] < fl.iloc[i] else 1
        else:
            st_dir[i] = 1 if close.iloc[i] > fu.iloc[i] else -1
        st_line[i] = fl.iloc[i] if st_dir[i] == 1 else fu.iloc[i]

    return pd.Series(st_dir, index=close.index), pd.Series(st_line, index=close.index)


# ── Session VWAP (daily anchored) ────────────────────────────────────────────

def _session_vwap(df: pd.DataFrame) -> Optional[float]:
    try:
        today = df.index[-1].normalize()
        session = df[df.index >= today]
        if len(session) < 3:  # need a few candles; else use last 24h
            session = df.iloc[-min(len(df), 96):]
        tp = (session["high"] + session["low"] + session["close"]) / 3
        v = session["volume"]
        if v.sum() == 0:
            return None
        return float((tp * v).sum() / v.sum())
    except Exception:
        return None


# ── Swing points / S-R ────────────────────────────────────────────────────────

def _detect_swing_points(high: pd.Series, low: pd.Series, pivot: int = 3):
    swing_highs, swing_lows = [], []
    n = len(high)
    for i in range(pivot, n - pivot):
        if all(high.iloc[i] >= high.iloc[i-j] for j in range(1, pivot+1)) and \
           all(high.iloc[i] >= high.iloc[i+j] for j in range(1, pivot+1)):
            swing_highs.append((i, float(high.iloc[i])))
        if all(low.iloc[i] <= low.iloc[i-j] for j in range(1, pivot+1)) and \
           all(low.iloc[i] <= low.iloc[i+j] for j in range(1, pivot+1)):
            swing_lows.append((i, float(low.iloc[i])))
    return swing_highs, swing_lows


def _nearest_sr(price: float, swing_highs, swing_lows):
    res = sorted([h for _, h in swing_highs if h > price])
    sup = sorted([l for _, l in swing_lows if l < price], reverse=True)
    return (res[0] if res else None), (sup[0] if sup else None)


def _price_structure(swing_highs, swing_lows):
    bull = bear = False
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        hh = swing_highs[-1][1] > swing_highs[-2][1]
        hl = swing_lows[-1][1] > swing_lows[-2][1]
        lh = swing_highs[-1][1] < swing_highs[-2][1]
        ll = swing_lows[-1][1] < swing_lows[-2][1]
        bull = hh and hl
        bear = lh and ll
    return bull, bear


# ── Candle patterns (on last closed candle) ──────────────────────────────────

def _candle_patterns(df: pd.DataFrame) -> dict:
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    o1, c1 = float(o.iloc[-1]), float(c.iloc[-1])
    o2, c2 = float(o.iloc[-2]), float(c.iloc[-2])
    h1, l1 = float(h.iloc[-1]), float(l.iloc[-1])
    rng = h1 - l1
    body = abs(c1 - o1)

    bull_engulf = (c2 < o2) and (c1 > o1) and (c1 >= o2) and (o1 <= c2) and body > abs(c2 - o2)
    bear_engulf = (c2 > o2) and (c1 < o1) and (c1 <= o2) and (o1 >= c2) and body > abs(c2 - o2)

    lower_wick = min(o1, c1) - l1
    upper_wick = h1 - max(o1, c1)
    bull_pin = rng > 0 and lower_wick >= 2 * body and upper_wick <= body and lower_wick / rng > 0.6
    bear_pin = rng > 0 and upper_wick >= 2 * body and lower_wick <= body and upper_wick / rng > 0.6

    return {
        "bull_engulf": bull_engulf, "bear_engulf": bear_engulf,
        "bull_pin": bull_pin, "bear_pin": bear_pin,
        "body_pct": body / rng if rng else 0,
        "candle_bull": c1 > o1,
    }


# ══════════════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame, drop_last: bool = True) -> Optional[dict]:
    """
    Compute all indicators on an OHLCV DataFrame.
    drop_last=True removes the currently-forming candle so every value is
    based on closed candles only.
    """
    if df is None or len(df) < 60:
        return None
    if drop_last:
        df = df.iloc[:-1]
        if len(df) < 60:
            return None

    try:
        close, high, low = df["close"], df["high"], df["low"]
        volume, open_ = df["volume"], df["open"]
        price = float(close.iloc[-1])

        # RSI
        rsi_series = tam.RSIIndicator(close, window=RSI_PERIOD).rsi()
        rsi = _safe(rsi_series)
        rsi_prev = _safe(rsi_series, -2)

        # MACD
        macd_obj = tat.MACD(close, window_fast=MACD_FAST, window_slow=MACD_SLOW, window_sign=MACD_SIGNAL)
        macd_line, macd_sig = _safe(macd_obj.macd()), _safe(macd_obj.macd_signal())
        macd_hist, macd_hist_prev = _safe(macd_obj.macd_diff()), _safe(macd_obj.macd_diff(), -2)
        macd_prev, macd_sig_p = _safe(macd_obj.macd(), -2), _safe(macd_obj.macd_signal(), -2)
        vals = [macd_line, macd_sig, macd_prev, macd_sig_p]
        macd_cross_bull = all(v is not None for v in vals) and macd_line > macd_sig and macd_prev <= macd_sig_p
        macd_cross_bear = all(v is not None for v in vals) and macd_line < macd_sig and macd_prev >= macd_sig_p
        macd_hist_rising = (macd_hist is not None and macd_hist_prev is not None and macd_hist > macd_hist_prev)

        # Bollinger
        bb = tav.BollingerBands(close, window=BB_PERIOD, window_dev=BB_STD)
        bb_upper, bb_mid, bb_lower = _safe(bb.bollinger_hband()), _safe(bb.bollinger_mavg()), _safe(bb.bollinger_lband())
        bb_pct = _safe(bb.bollinger_pband())
        bbw_series = bb.bollinger_wband()
        bb_width = _safe(bbw_series)
        bbw_pctile = _pctile_of_last(bbw_series)

        # EMAs
        ema9  = _safe(tat.EMAIndicator(close, window=EMA_FAST).ema_indicator())
        ema21 = _safe(tat.EMAIndicator(close, window=EMA_MID).ema_indicator())
        ema50 = _safe(tat.EMAIndicator(close, window=EMA_SLOW).ema_indicator())
        ema200_series = tat.EMAIndicator(close, window=EMA_TREND).ema_indicator() if len(df) >= 200 else None
        ema200 = _safe(ema200_series) if ema200_series is not None else None
        ema200_prev = _safe(ema200_series, -5) if ema200_series is not None else None
        ema200_slope = ((ema200 - ema200_prev) / ema200_prev * 100) if (ema200 and ema200_prev) else None

        ema_bull = all(v is not None for v in (ema9, ema21, ema50)) and ema9 > ema21 > ema50
        ema_bear = all(v is not None for v in (ema9, ema21, ema50)) and ema9 < ema21 < ema50
        above_200 = (price > ema200) if ema200 is not None else None

        # ADX / DI
        adx_obj = tat.ADXIndicator(high, low, close, window=ADX_PERIOD)
        adx, di_pos, di_neg = _safe(adx_obj.adx()), _safe(adx_obj.adx_pos()), _safe(adx_obj.adx_neg())
        adx_bull = adx is not None and di_pos is not None and di_neg is not None and di_pos > di_neg
        adx_bear = adx is not None and di_pos is not None and di_neg is not None and di_neg > di_pos

        # ATR
        atr_series = tav.AverageTrueRange(high, low, close, window=ATR_PERIOD).average_true_range()
        atr = _safe(atr_series)
        atr_pct = (atr / price * 100) if (atr and price) else None
        atr_pctile = _pctile_of_last(atr_series / close)

        # Stochastic
        st = tam.StochasticOscillator(high, low, close, window=STOCH_K, smooth_window=STOCH_D)
        stoch_k, stoch_d = _safe(st.stoch()), _safe(st.stoch_signal())

        # MFI / CMF / OBV
        mfi = _safe(tavo.MFIIndicator(high, low, close, volume, window=MFI_PERIOD).money_flow_index())
        cmf = _safe(tavo.ChaikinMoneyFlowIndicator(high, low, close, volume, window=CMF_PERIOD).chaikin_money_flow())
        obv_series = tavo.OnBalanceVolumeIndicator(close, volume).on_balance_volume()
        obv, obv_ma = _safe(obv_series), _safe(obv_series.rolling(OBV_MA_PERIOD).mean())
        obv_rising = (obv > obv_ma) if (obv is not None and obv_ma is not None) else None

        # SuperTrend
        st_dir_series, st_line_series = _supertrend(high, low, close)
        st_dir = int(st_dir_series.iloc[-1])
        st_line = _safe(st_line_series)
        st_flip_bull = st_dir == 1 and int(st_dir_series.iloc[-2]) == -1
        st_flip_bear = st_dir == -1 and int(st_dir_series.iloc[-2]) == 1

        # Donchian
        don_high = float(high.rolling(DONCHIAN_PERIOD).max().iloc[-2])  # exclude current bar
        don_low  = float(low.rolling(DONCHIAN_PERIOD).min().iloc[-2])

        # Volume
        vol_ma20 = float(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else 0
        vol_current = float(volume.iloc[-1])
        vol_ratio = vol_current / vol_ma20 if vol_ma20 else 1.0
        vol_spike = vol_ratio > 1.5

        # Candle patterns
        patterns = _candle_patterns(df)

        # Session VWAP
        vwap = _session_vwap(df)
        above_vwap = (price > vwap) if vwap is not None else None

        # Swing points / S-R / structure
        lb = min(SR_LOOKBACK_CANDLES, len(df) - 5)
        sh, sl_pts = _detect_swing_points(high.iloc[-lb:].reset_index(drop=True),
                                          low.iloc[-lb:].reset_index(drop=True))
        nearest_resistance, nearest_support = _nearest_sr(price, sh, sl_pts)
        structure_bull, structure_bear = _price_structure(sh, sl_pts)
        last_swing_low  = sl_pts[-1][1] if sl_pts else None
        last_swing_high = sh[-1][1] if sh else None

        # Pullback detection: did price touch EMA21 zone in the last 3 bars?
        touched_ema21 = False
        if ema21 is not None and atr:
            recent_lows = low.iloc[-3:]
            recent_highs = high.iloc[-3:]
            touched_ema21 = bool(
                (recent_lows.min() <= ema21 + 0.3 * atr and price > ema21) or
                (recent_highs.max() >= ema21 - 0.3 * atr and price < ema21)
            )

        return {
            "price": price,
            "prev_close": float(close.iloc[-2]),
            "change_pct": (price - float(close.iloc[-2])) / float(close.iloc[-2]) * 100,

            "rsi": rsi, "rsi_prev": rsi_prev,
            "macd_line": macd_line, "macd_signal": macd_sig,
            "macd_hist": macd_hist, "macd_hist_rising": macd_hist_rising,
            "macd_cross_bull": macd_cross_bull, "macd_cross_bear": macd_cross_bear,

            "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower,
            "bb_pct": bb_pct, "bb_width": bb_width, "bbw_pctile": bbw_pctile,

            "ema9": ema9, "ema21": ema21, "ema50": ema50, "ema200": ema200,
            "ema200_slope": ema200_slope,
            "ema_bull": ema_bull, "ema_bear": ema_bear, "above_200": above_200,
            "touched_ema21": touched_ema21,

            "adx": adx, "di_pos": di_pos, "di_neg": di_neg,
            "adx_bull": adx_bull, "adx_bear": adx_bear,

            "atr": atr, "atr_pct": atr_pct, "atr_pctile": atr_pctile,

            "stoch_k": stoch_k, "stoch_d": stoch_d,
            "mfi": mfi, "cmf": cmf,
            "obv_rising": obv_rising,

            "supertrend_dir": st_dir, "supertrend_line": st_line,
            "st_flip_bull": st_flip_bull, "st_flip_bear": st_flip_bear,

            "donchian_high": don_high, "donchian_low": don_low,

            "vol_ratio": vol_ratio, "vol_spike": vol_spike,

            "vwap": vwap, "above_vwap": above_vwap,

            "nearest_resistance": nearest_resistance,
            "nearest_support": nearest_support,
            "last_swing_low": last_swing_low,
            "last_swing_high": last_swing_high,
            "structure_bull": structure_bull, "structure_bear": structure_bear,

            **patterns,
        }

    except Exception as e:
        logger.error(f"Indicator computation error: {e}")
        return None
