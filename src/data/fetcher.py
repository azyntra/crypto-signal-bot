"""
fetcher.py — Async multi-exchange OHLCV fetcher using ccxt.
Supports Binance, Bybit, OKX, KuCoin for both spot and futures.
"""
import asyncio
from typing import Optional

import ccxt.async_support as ccxt
import pandas as pd

from config.logger import get_logger
from config.settings import (
    EXCHANGE_CREDENTIALS, CANDLE_LIMIT, QUOTE_CURRENCY, MIN_VOLUME_USDT
)

logger = get_logger(__name__)

# Exchange instances — created once, reused
_exchanges: dict[str, ccxt.Exchange] = {}


def _make_exchange(name: str, market_type: str = "spot") -> ccxt.Exchange:
    """Build a ccxt exchange instance for spot or futures."""
    creds = EXCHANGE_CREDENTIALS.get(name, {}).copy()

    # Override market type
    if market_type == "spot":
        creds["options"] = {"defaultType": "spot"}
    elif market_type == "futures":
        opts = {
            "binance": {"defaultType": "future"},
            "bybit":   {"defaultType": "linear"},
            "okx":     {"defaultType": "swap"},
            "kucoin":  {"defaultType": "future"},
        }
        creds["options"] = opts.get(name, {"defaultType": "future"})

    # FIX: OKX requires a 'password' (passphrase) field to be present,
    # even when using public/unauthenticated endpoints. Without it,
    # ccxt's OKX implementation tries to concatenate None + str when
    # building the request URL, causing:
    #   "unsupported operand type(s) for +: 'NoneType' and 'str'"
    if name == "okx":
        creds.setdefault("apiKey", "")
        creds.setdefault("secret", "")
        creds.setdefault("password", "")  # OKX passphrase — must not be None

    cls = getattr(ccxt, name, None)
    if cls is None:
        raise ValueError(f"Unknown exchange: {name}")
    return cls(creds)


async def get_exchange(name: str, market_type: str = "spot") -> ccxt.Exchange:
    key = f"{name}_{market_type}"
    if key not in _exchanges:
        _exchanges[key] = _make_exchange(name, market_type)
    return _exchanges[key]


async def close_all():
    for ex in _exchanges.values():
        await ex.close()
    _exchanges.clear()


async def fetch_ohlcv(
    exchange_name: str,
    symbol: str,
    timeframe: str,
    market_type: str = "spot",
    limit: int = CANDLE_LIMIT,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles and return as a DataFrame with columns:
    timestamp, open, high, low, close, volume
    Returns None on failure.
    """
    try:
        ex = await get_exchange(exchange_name, market_type)
        raw = await ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not raw or len(raw) < 50:
            return None
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)
        return df
    except ccxt.NetworkError as e:
        logger.warning(f"[{exchange_name}] Network error fetching {symbol} {timeframe}: {e}")
    except ccxt.ExchangeError as e:
        logger.debug(f"[{exchange_name}] Exchange error for {symbol} {timeframe}: {e}")
    except Exception as e:
        logger.error(f"[{exchange_name}] Unexpected error for {symbol} {timeframe}: {e}")
    return None


async def get_exchange_symbols(exchange_name: str, market_type: str = "spot") -> set[str]:
    """Return all USDT-quoted symbols listed on the exchange."""
    try:
        ex = await get_exchange(exchange_name, market_type)
        markets = await ex.load_markets()
        return {
            sym for sym, m in markets.items()
            if m.get("quote") == QUOTE_CURRENCY and m.get("active", True)
        }
    except Exception as e:
        logger.error(f"[{exchange_name}] Failed to load markets: {e}")
        return set()


async def get_24h_volume(exchange_name: str, symbol: str, market_type: str = "spot") -> float:
    """Return 24h volume in USDT. Returns 0 on failure."""
    try:
        ex = await get_exchange(exchange_name, market_type)
        ticker = await ex.fetch_ticker(symbol)
        return float(ticker.get("quoteVolume") or ticker.get("baseVolume", 0))
    except Exception:
        return 0.0


async def fetch_multi_timeframe(
    exchange_name: str,
    symbol: str,
    timeframes: list[str],
    market_type: str = "spot",
) -> dict[str, pd.DataFrame]:
    """
    Fetch multiple timeframes concurrently for a single symbol.
    Returns {timeframe: DataFrame}
    """
    tasks = {
        tf: fetch_ohlcv(exchange_name, symbol, tf, market_type)
        for tf in timeframes
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    out = {}
    for tf, result in zip(tasks.keys(), results):
        if isinstance(result, pd.DataFrame):
            out[tf] = result
    return out
