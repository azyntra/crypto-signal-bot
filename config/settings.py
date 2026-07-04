"""
settings.py — Central configuration for Crypto Signal Bot v3.
All tuneable parameters live here.
"""
import os
from dotenv import load_dotenv

load_dotenv()

VERSION = "3.0.0"

# ── Delivery ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID  = os.getenv("TELEGRAM_CHANNEL_ID")
TELEGRAM_ADMIN_ID    = os.getenv("TELEGRAM_ADMIN_ID")

MAX_SIGNALS_PER_HOUR   = int(os.getenv("MAX_SIGNALS_PER_HOUR", 6))
MAX_OPEN_SIGNALS       = int(os.getenv("MAX_OPEN_SIGNALS", 10))
MAX_OPEN_PER_SYMBOL    = 1
SEND_CHART_IMAGES      = True     # attach chart PNG to each signal

# ── AI (Gemini) ───────────────────────────────────────────────────────────────
GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY")
AI_FILTER_ENABLED    = True
AI_MODEL             = os.getenv("AI_MODEL", "gemini-2.0-flash")
AI_DAILY_BRIEF       = True       # post daily AI market outlook at 08:05 UTC

# ── ML predictor ──────────────────────────────────────────────────────────────
ML_PREDICTOR_ENABLED    = os.getenv("ML_PREDICTOR_ENABLED", "false").lower() == "true"
ML_MIN_TRAINING_SIGNALS = 80
ML_MIN_WIN_PROB         = 0.45

# ── Exchanges ─────────────────────────────────────────────────────────────────
EXCHANGE_CREDENTIALS = {
    "binance": {
        "apiKey": os.getenv("BINANCE_API_KEY", ""),
        "secret": os.getenv("BINANCE_SECRET", ""),
    },
    "bybit": {
        "apiKey": os.getenv("BYBIT_API_KEY", ""),
        "secret": os.getenv("BYBIT_SECRET", ""),
    },
}

# Scan futures only: signals work for both spot (LONG) and futures traders,
# and it kills the spot/futures duplicate-signal problem of v2.
SCAN_EXCHANGES = ["binance"]          # data source for scanning
TRACK_EXCHANGE = "binance"            # data source for outcome tracking
MARKET_TYPE    = "futures"
QUOTE_CURRENCY = "USDT"

# ── Market scan ───────────────────────────────────────────────────────────────
TOP_N_COINS        = int(os.getenv("TOP_N_COINS", 150))
MAX_TRADABLE_COINS = int(os.getenv("MAX_TRADABLE_COINS", 60))
MIN_VOLUME_USDT    = float(os.getenv("MIN_VOLUME_USDT", 10_000_000))
SCAN_CONCURRENCY   = 8                # parallel symbol scans

# ── Timeframes ────────────────────────────────────────────────────────────────
# v3 drops 1m/5m scalping entirely: intraday = 15m entry / 1h+4h context,
# swing = 1h entry / 4h+1d context.
INTRADAY_TIMEFRAMES = ["15m", "1h", "4h"]
SWING_TIMEFRAMES    = ["1h", "4h", "1d"]
CANDLE_LIMIT        = 300

INTRADAY_SCAN_INTERVAL_MIN = 15
SWING_SCAN_INTERVAL_MIN    = 60
TOP_COINS_REFRESH_MIN      = 60

# ── Signal quality gates ──────────────────────────────────────────────────────
MIN_CONFIDENCE       = 65      # publish threshold (post-AI, post-adaptive)
MIN_RR_RATIO         = 1.5     # measured at TP2
ADX_TREND_MIN        = 22      # trend regime requires this on 4h
ADX_RANGE_MAX        = 18      # range regime requires ADX below this
BBW_PCTILE_SQUEEZE   = 15      # BB width percentile counted as squeeze
BBW_PCTILE_RANGE     = 40      # BB width percentile ceiling for range regime

# ── BTC regime filter ─────────────────────────────────────────────────────────
BTC_FILTER_ENABLED   = True
BTC_SYMBOL           = "BTC/USDT"
BTC_REGIME_CACHE_MIN = 15
# Circuit breaker: pause new signals when BTC moves violently
BTC_SHOCK_PCT        = 1.5     # abs % move in 15 minutes
BTC_SHOCK_PAUSE_MIN  = 30

# ── News / event guard ────────────────────────────────────────────────────────
NEWS_GUARD_ENABLED   = True
EVENTS_FILE          = "data/events.json"   # manual high-impact events (UTC)
EVENT_BLOCK_MIN      = 45                   # block ± this many minutes around event

# ── Indicator parameters ──────────────────────────────────────────────────────
RSI_PERIOD    = 14
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9
BB_PERIOD     = 20
BB_STD        = 2.0
EMA_FAST      = 9
EMA_MID       = 21
EMA_SLOW      = 50
EMA_TREND     = 200
ADX_PERIOD    = 14
ATR_PERIOD    = 14
STOCH_K       = 14
STOCH_D       = 3
MFI_PERIOD    = 14
CMF_PERIOD    = 20
OBV_MA_PERIOD = 20
SUPERTREND_PERIOD = 10
SUPERTREND_MULT   = 3.0
DONCHIAN_PERIOD   = 20
SR_LOOKBACK_CANDLES = 60

# ── Risk / targets ────────────────────────────────────────────────────────────
ATR_SL_BUFFER   = 0.5    # extra ATR beyond structure for SL
MIN_SL_ATR      = 1.0    # SL never tighter than 1 ATR
MAX_SL_PCT      = 5.0    # reject setups needing > 5% stop
TP1_R = 1.5
TP2_R = 2.5
TP3_R = 4.0
# Exit model used for honest R accounting: close 1/3 at each TP,
# SL→breakeven after TP1, SL→TP1 after TP2.
TP_PORTIONS = (1/3, 1/3, 1/3)

# ── Entry fill logic ──────────────────────────────────────────────────────────
ENTRY_ZONE_ATR      = 0.25   # entry zone half-width in ATR
FILL_EXPIRY_HOURS   = {"intraday": 2, "swing": 8}    # cancel if never filled
TRADE_EXPIRY_HOURS  = {"intraday": 24, "swing": 96}  # force-close stale trades

# ── Funding rate sentiment (futures crowding) ─────────────────────────────────
FUNDING_ENABLED       = True
FUNDING_EXTREME       = 0.0008   # |rate| above this = crowded side, penalize
FUNDING_PENALTY       = 8        # confidence penalty

# ── Sentiment (Fear & Greed) ──────────────────────────────────────────────────
SENTIMENT_ENABLED       = True
SENTIMENT_EXTREME_BOOST = 6

# ── Adaptive feedback ─────────────────────────────────────────────────────────
ADAPTIVE_CONFIDENCE    = True
ADAPTIVE_LOOKBACK_DAYS = 14
ADAPTIVE_MIN_SIGNALS   = 10
LOSS_STREAK_PAUSE        = 4     # pause after N consecutive full-SL losses
LOSS_STREAK_COOLDOWN_MIN = 120

# ── Deduplication ─────────────────────────────────────────────────────────────
DEDUP_WINDOW_MIN = {"intraday": 120, "swing": 480}   # per symbol+direction, cross-exchange

# ── Backtest ──────────────────────────────────────────────────────────────────
BACKTEST_MAX_DAYS = 90

# ── Database / logging ────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///signals.db")
LOG_LEVEL    = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE     = "logs/bot.log"

# ── Coin universe ─────────────────────────────────────────────────────────────
SMART_COIN_SELECTION = True

# ── Backwards-compat aliases (v2 modules/scripts) ─────────────────────────────
MIN_CONFIDENCE_SCALP = MIN_CONFIDENCE
MIN_CONFIDENCE_SWING = MIN_CONFIDENCE
SPOT_EXCHANGE        = "binance"
