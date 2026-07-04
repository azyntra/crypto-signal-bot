"""
regime.py — Market regime engine (v3).

Three layers:
  1. Per-coin regime (4h): trend_up / trend_down / range / choppy.
     Strategies are GATED on this — choppy = no signals, period.
  2. Global BTC regime + shock circuit breaker: block alt longs when BTC
     is dumping, block alt shorts when BTC is pumping, pause everything
     for a violent BTC move.
  3. News guard: block signals around scheduled high-impact events
     (manual events.json — FOMC, CPI, etc.).
"""
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from src.data.fetcher import fetch_ohlcv
from src.analysis.indicators import compute_indicators
from config.logger import get_logger
from config.settings import (
    ADX_TREND_MIN, ADX_RANGE_MAX, BBW_PCTILE_RANGE,
    BTC_FILTER_ENABLED, BTC_SYMBOL, BTC_REGIME_CACHE_MIN,
    BTC_SHOCK_PCT, BTC_SHOCK_PAUSE_MIN,
    NEWS_GUARD_ENABLED, EVENTS_FILE, EVENT_BLOCK_MIN,
    TRACK_EXCHANGE, MARKET_TYPE,
)

logger = get_logger(__name__)

_btc_cache = {"data": None, "at": 0.0}
_shock_until = 0.0


# ── Per-coin regime ───────────────────────────────────────────────────────────

def classify_regime(ind_4h: Optional[dict], ind_1h: Optional[dict] = None) -> str:
    """
    Classify a coin's regime from its 4h indicators (1h used as tiebreaker).
    Returns: "trend_up" | "trend_down" | "range" | "choppy"
    """
    if not ind_4h:
        return "choppy"

    adx = ind_4h.get("adx") or 0
    bbw_pct = ind_4h.get("bbw_pctile")
    above_200 = ind_4h.get("above_200")
    slope = ind_4h.get("ema200_slope")
    st_dir = ind_4h.get("supertrend_dir", 0)

    # Trend: ADX strong + direction agreement between DI, SuperTrend, EMA200 side
    if adx >= ADX_TREND_MIN:
        bull_votes = sum([
            bool(ind_4h.get("adx_bull")),
            st_dir == 1,
            above_200 is True,
            (slope or 0) > 0.05,
            bool(ind_4h.get("ema_bull")),
        ])
        bear_votes = sum([
            bool(ind_4h.get("adx_bear")),
            st_dir == -1,
            above_200 is False,
            (slope or 0) < -0.05,
            bool(ind_4h.get("ema_bear")),
        ])
        if bull_votes >= 3 and bull_votes > bear_votes:
            return "trend_up"
        if bear_votes >= 3 and bear_votes > bull_votes:
            return "trend_down"
        return "choppy"

    # Range: weak ADX + compressed bands
    if adx <= ADX_RANGE_MAX and bbw_pct is not None and bbw_pct <= BBW_PCTILE_RANGE:
        return "range"

    return "choppy"


# ── BTC regime ────────────────────────────────────────────────────────────────

async def get_btc_regime() -> dict:
    """
    Returns {"regime": str, "shock": bool, "change_1h": float, "rsi_1h": float}
    Cached for BTC_REGIME_CACHE_MIN minutes.
    """
    global _shock_until
    now = time.time()
    if _btc_cache["data"] and now - _btc_cache["at"] < BTC_REGIME_CACHE_MIN * 60:
        data = dict(_btc_cache["data"])
        data["shock"] = now < _shock_until
        return data

    out = {"regime": "unknown", "shock": False, "change_1h": 0.0, "rsi_1h": None}
    try:
        df_4h = await fetch_ohlcv(TRACK_EXCHANGE, BTC_SYMBOL, "4h", MARKET_TYPE)
        df_1h = await fetch_ohlcv(TRACK_EXCHANGE, BTC_SYMBOL, "1h", MARKET_TYPE)
        df_5m = await fetch_ohlcv(TRACK_EXCHANGE, BTC_SYMBOL, "5m", MARKET_TYPE, limit=60, use_cache=False)

        ind_4h = compute_indicators(df_4h)
        ind_1h = compute_indicators(df_1h)
        out["regime"] = classify_regime(ind_4h, ind_1h)
        if ind_1h:
            out["rsi_1h"] = ind_1h.get("rsi")
            out["change_1h"] = ind_1h.get("change_pct", 0.0)

        # Shock detection: |move| over last 15 minutes (3 × 5m closed candles)
        if df_5m is not None and len(df_5m) > 4:
            closes = df_5m["close"]
            move = (float(closes.iloc[-1]) - float(closes.iloc[-4])) / float(closes.iloc[-4]) * 100
            if abs(move) >= BTC_SHOCK_PCT:
                _shock_until = now + BTC_SHOCK_PAUSE_MIN * 60
                logger.warning(f"BTC shock: {move:+.2f}% in 15m — pausing signals {BTC_SHOCK_PAUSE_MIN}m")

        out["shock"] = now < _shock_until
        _btc_cache["data"] = {k: v for k, v in out.items() if k != "shock"}
        _btc_cache["at"] = now
    except Exception as e:
        logger.error(f"BTC regime error: {e}")
    return out


def btc_blocks_direction(btc: dict, direction: str) -> Optional[str]:
    """
    Return a human-readable block reason if the BTC filter vetoes this
    direction, else None.
    """
    if not BTC_FILTER_ENABLED or not btc:
        return None
    if btc.get("shock"):
        return "BTC shock move — circuit breaker active"

    regime = btc.get("regime")
    rsi = btc.get("rsi_1h")

    if direction == "LONG" and regime == "trend_down" and (rsi is None or rsi < 45):
        return "BTC in downtrend — alt longs blocked"
    if direction == "SHORT" and regime == "trend_up" and (rsi is None or rsi > 55):
        return "BTC in uptrend — alt shorts blocked"
    return None


# ── News / event guard ────────────────────────────────────────────────────────

def _load_events() -> list[dict]:
    if not os.path.exists(EVENTS_FILE):
        return []
    try:
        with open(EVENTS_FILE) as f:
            return json.load(f).get("events", [])
    except Exception as e:
        logger.warning(f"Could not read events file: {e}")
        return []


def add_event(name: str, when_utc: str) -> bool:
    """Add an event. when_utc format: 'YYYY-MM-DD HH:MM'."""
    try:
        datetime.strptime(when_utc, "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    events = _load_events()
    events.append({"name": name, "time_utc": when_utc})
    os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
    with open(EVENTS_FILE, "w") as f:
        json.dump({"events": events}, f, indent=2)
    return True


def get_upcoming_events(hours: int = 48) -> list[dict]:
    now = datetime.now(timezone.utc)
    out = []
    for ev in _load_events():
        try:
            t = datetime.strptime(ev["time_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            delta_h = (t - now).total_seconds() / 3600
            if -1 <= delta_h <= hours:
                out.append({**ev, "dt": t})
        except Exception:
            continue
    return sorted(out, key=lambda e: e["dt"])


def news_guard_active() -> Optional[str]:
    """Return event name if we're inside the block window of any event."""
    if not NEWS_GUARD_ENABLED:
        return None
    now = datetime.now(timezone.utc)
    for ev in _load_events():
        try:
            t = datetime.strptime(ev["time_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            if abs((t - now).total_seconds()) <= EVENT_BLOCK_MIN * 60:
                return ev.get("name", "scheduled event")
        except Exception:
            continue
    return None
