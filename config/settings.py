"""
settings.py — Central configuration for the Crypto Signal Bot.
All tuneable parameters live here. Edit freely.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Delivery ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID     = os.getenv("TELEGRAM_CHANNEL_ID")
TELEGRAM_ADMIN_ID       = os.getenv("TELEGRAM_ADMIN_ID")
MAX_SIGNALS_PER_HOUR    = int(os.getenv("MAX_SIGNALS_PER_HOUR", 10))

# ── AI & ML Settings ──────────────────────────────────────────────────────────
# Phase 1: Gemini AI Filter
GEMINI_API_KEY          = os.getenv("GEMINI_API_KEY")
AI_FILTER_ENABLED       = True
AI_MODEL                = "gemini-2.5-flash"

# Phase 2: ML Predictor
ML_PREDICTOR_ENABLED    = False  # Enable after 50+ closed signals
ML_MIN_TRAINING_SIGNALS = 50

# Phase 3: Market Sentiment (Fear & Greed)
SENTIMENT_ENABLED       = True
SENTIMENT_EXTREME_BOOST = 10

# ── Exchange API keys ──────────────────────────────────────────────────────────
EXCHANGE_CREDENTIALS = {
    "binance": {
        "apiKey": os.getenv("BINANCE_API_KEY", ""),
        "secret": os.getenv("BINANCE_SECRET", ""),
        "options": {"defaultType": "future"},
    },
    "bybit": {
        "apiKey": os.getenv("BYBIT_API_KEY", ""),
        "secret": os.getenv("BYBIT_SECRET", ""),
        "options": {"defaultType": "linear"},
    },
    "okx": {
        "apiKey": os.getenv("OKX_API_KEY", ""),
        "secret": os.getenv("OKX_SECRET", ""),
        "password": os.getenv("OKX_PASSPHRASE", ""),
        "options": {"defaultType": "swap"},
    },
    "kucoin": {
        "apiKey": os.getenv("KUCOIN_API_KEY", ""),
        "secret": os.getenv("KUCOIN_SECRET", ""),
        "password": os.getenv("KUCOIN_PASSPHRASE", ""),
        "options": {"defaultType": "future"},
    },
}

# Primary exchange for spot market data (public, no key needed)
SPOT_EXCHANGE = "binance"

# ── Market scan settings ───────────────────────────────────────────────────────
TOP_N_COINS          = int(os.getenv("TOP_N_COINS", 150))
MIN_VOLUME_USDT      = float(os.getenv("MIN_VOLUME_USDT", 500_000))
MAX_SIGNALS_PER_HOUR = int(os.getenv("MAX_SIGNALS_PER_HOUR", 20))
QUOTE_CURRENCY       = "USDT"

# ── Signal quality thresholds ──────────────────────────────────────────────────
MIN_CONFIDENCE_SCALP = 55.0
MIN_CONFIDENCE_SWING = 60.0
MIN_RR_RATIO         = 1.5
MIN_INDICATORS_AGREE = 2      # minimum number of indicators that must agree

# ── Indicator parameters ───────────────────────────────────────────────────────
RSI_PERIOD           = 14
RSI_OVERSOLD         = 30
RSI_OVERBOUGHT       = 70
MACD_FAST            = 12
MACD_SLOW            = 26
MACD_SIGNAL          = 9
BB_PERIOD            = 20
BB_STD               = 2.0
EMA_FAST             = 9
EMA_MID              = 21
EMA_SLOW             = 50
EMA_TREND            = 200
ADX_PERIOD           = 14
ADX_TREND_MIN        = 20    # ADX > this = trending market
ATR_PERIOD           = 14
ATR_SL_MULTIPLIER    = 1.5   # stop loss = entry ± (ATR × multiplier) — scalp
ATR_SL_MULTIPLIER_SWING = 2.5  # wider SL for swing trades (4h noise)
ADX_SWING_MIN        = 20     # stronger trend required for swing signals
STOCH_K              = 14
STOCH_D              = 3
STOCH_OVERSOLD       = 25
STOCH_OVERBOUGHT     = 75
OBV_MA_PERIOD        = 20

# ── Signal filters ─────────────────────────────────────────────────────────────
COUNTER_TREND_BLOCK  = False  # penalty instead of block for EMA200 macro trend
SWING_HTF_REQUIRED   = True   # mandate higher-TF confirmation for swing signals
MIN_VOLUME_USDT_SWING = float(os.getenv("MIN_VOLUME_USDT_SWING", 1_000_000))

# ── Timeframes ─────────────────────────────────────────────────────────────────
SCALPING_TIMEFRAMES  = ["1m", "5m", "15m"]
SWING_TIMEFRAMES     = ["1h", "4h", "1d"]
CANDLE_LIMIT         = 200   # number of candles to fetch per timeframe

# ── Scheduling ─────────────────────────────────────────────────────────────────
SCALP_SCAN_INTERVAL_MIN  = 5     # run scalping scan every N minutes
SWING_SCAN_INTERVAL_MIN  = 60    # run swing scan every N minutes
TOP_COINS_REFRESH_MIN    = 30    # refresh top-100 coin list every N minutes

# ── Risk / TP levels ───────────────────────────────────────────────────────────
# TP distances as multiples of the SL distance (R multiples)
TP1_R = 1.0
TP2_R = 2.0
TP3_R = 3.5

# ── Trailing stop / partial close ──────────────────────────────────────────────
TRAILING_STOP_ENABLED   = True
BREAKEVEN_TRIGGER       = 0.5    # move SL to entry when price reaches 50% of TP1
# When TP1 hit → SL moves to 50% between entry and TP1
# When TP2 hit → SL moves to TP1
# When TP3 hit → close fully

# ── Cooldown after consecutive losses ─────────────────────────────────────────
LOSS_STREAK_PAUSE       = 10      # pause after N consecutive SL hits
LOSS_STREAK_COOLDOWN_MIN = 0     # cooldown duration in minutes

# ── Market regime thresholds ──────────────────────────────────────────────────
# Trending: ADX > 25, BB width > 0.06 → trend-following signals only
# Ranging:  ADX < 20, BB width < 0.04 → mean-reversion only
# Choppy:   everything else → NO signals
REGIME_TRENDING_ADX     = 25     # ADX above this = trending
REGIME_RANGING_ADX      = 20     # ADX below this = ranging
REGIME_TRENDING_BBW     = 0.06   # BB width above this supports trend
REGIME_RANGING_BBW      = 0.04   # BB width below this supports range

# ── Adaptive confidence (win-rate feedback loop) ──────────────────────────────
ADAPTIVE_CONFIDENCE     = False    # disabled: re-enable after 24-48h of clean data
ADAPTIVE_LOOKBACK_DAYS  = 7      # look at last N days of performance
ADAPTIVE_MIN_SIGNALS    = 5      # need at least N signals before applying penalty
ADAPTIVE_BLOCK_WINRATE  = 10     # block direction if win rate < this %

# ── Support / Resistance ──────────────────────────────────────────────────────
SR_LOOKBACK_CANDLES     = 20     # candles to look back for swing highs/lows
SR_BLOCK_PROXIMITY_PCT  = 0.5    # block signal if < this % from S/R level
SR_PENALTY_PROXIMITY_PCT = 1.5   # reduce score if < this % from S/R level

# ── Smart coin selection ──────────────────────────────────────────────────────
SMART_COIN_SELECTION    = True
MAX_TRADABLE_COINS      = 100     # only scan top N most tradable coins

# ── Database ───────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///signals.db")

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FILE  = "logs/bot.log"
