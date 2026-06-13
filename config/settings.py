"""
settings.py — Central configuration for the Crypto Signal Bot.
All tuneable parameters live here. Edit freely.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID  = os.getenv("TELEGRAM_CHANNEL_ID", "")   # set after creating channel
TELEGRAM_ADMIN_ID    = os.getenv("TELEGRAM_ADMIN_ID", "")

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
TOP_N_COINS          = int(os.getenv("TOP_N_COINS", 100))
MIN_VOLUME_USDT      = float(os.getenv("MIN_VOLUME_USDT", 1_000_000))
MAX_SIGNALS_PER_HOUR = int(os.getenv("MAX_SIGNALS_PER_HOUR", 10))
QUOTE_CURRENCY       = "USDT"

# ── Signal quality thresholds ──────────────────────────────────────────────────
MIN_CONFIDENCE       = float(os.getenv("MIN_CONFIDENCE", 70))
MIN_RR_RATIO         = float(os.getenv("MIN_RR_RATIO", 2.0))
MIN_INDICATORS_AGREE = 3      # minimum number of indicators that must agree

# ── Indicator parameters ───────────────────────────────────────────────────────
RSI_PERIOD           = 14
RSI_OVERSOLD         = 35
RSI_OVERBOUGHT       = 65
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
ATR_SL_MULTIPLIER    = 1.5   # stop loss = entry ± (ATR × multiplier)
STOCH_K              = 14
STOCH_D              = 3
STOCH_OVERSOLD       = 25
STOCH_OVERBOUGHT     = 75
OBV_MA_PERIOD        = 20

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

# ── Database ───────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///signals.db")

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FILE  = "logs/bot.log"
