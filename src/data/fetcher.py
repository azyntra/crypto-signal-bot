"""
fetcher.py — Async multi-exchange OHLCV fetcher using ccxt.
Supports Binance, Bybit, OKX, KuCoin for both spot and futures.

Fixes:
  - OKX: apiKey/secret/password must all be non-None strings (even empty)
    to prevent ccxt from crashing with "NoneType + str" on public endpoints.
  - Exchange instances are keyed and reused, but OKX gets explicit
    empty-string defaults for all auth fields.
"""
import asyncio
from typing import Optional

import ccxt.async_support as ccxt
import pandas as pd

from config.logger import get_logger
from config.settings import (
    EXCHANGE_CREDENTIALS, CANDLE_LIMIT, QUOTE_CURRENCY,
)

logger = get_logger(__name__)

_exchanges: dict[str, ccxt.Exchange] = {}


def _make_exchange(name: str, market_type: str = "spot") -> ccxt.Exchange:
    """Build a ccxt exchange instance for spot or futures."""
    import copy
    creds = copy.deepcopy(EXCHANGE_CREDENTIALS.get(name, {}))

    # Set market type
    if "options" not in creds:
        creds["options"] = {}

    if market_type == "spot":
        creds["options"]["defaultType"] = "spot"
    else:
        opts = {
            "binance": {"defaultType": "future"},
            "bybit":   {"defaultType": "linear"},
            "okx":     {"defaultType": "swap"},
            "kucoin":  {"defaultType": "future"},
        }
        creds["options"].update(opts.get(name, {"defaultType": "future"}))

    # OKX FIX: ccxt OKX client concatenates auth strings during request
    # signing even for public endpoints. Any None value causes:
    #   TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'
    # All three fields must be non-None strings (empty string is fine).
    if name == "okx":
        creds["apiKey"]   = creds.get("apiKey")   or ""
        creds["secret"]   = creds.get("secret")   or ""
        creds["password"] = creds.get("password") or ""

    cls = getattr(ccxt, name, None)
    if cls is None:
        raise ValueError(f"Unknown exchange: {name}")

    instance = cls(creds)

    # Disable verbose logging from ccxt itself
    instance.verbose = False

    return instance


async def get_exchange(name: str, market_type: str = "spot") -> ccxt.Exchange:
    key = f"{name}_{market_type}"
    if key not in _exchanges:
        _exchanges[key] = _make_exchange(name, market_type)
    return _exchanges[key]


async def close_all():
    for ex in _exchanges.values():
        try:
            await ex.close()
        except Exception:
            pass
    _exchanges.clear()


async def fetch_ohlcv(
    exchange_name: str,
    symbol: str,
    timeframe: str,
    market_type: str = "spot",
    limit: int = CANDLE_LIMIT,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles and return as a DataFrame.
    Returns None on any failure.
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
        logger.warning(f"[{exchange_name}] Network error {symbol} {timeframe}: {e}")
    except ccxt.ExchangeError as e:
        logger.debug(f"[{exchange_name}] Exchange error {symbol} {timeframe}: {e}")
    except Exception as e:
        logger.error(f"[{exchange_name}] Unexpected error {symbol} {timeframe}: {e}")
    return None


async def get_exchange_symbols(exchange_name: str, market_type: str = "spot") -> set[str]:
    """Return all active USDT-quoted symbols on the exchange."""
    try:
        ex = await get_exchange(exchange_name, market_type)
        markets = await ex.load_markets()
        return {
            sym for sym, m in markets.items()
            if m.get("quote") == QUOTE_CURRENCY and m.get("active", True)
        }
    except Exception as e:
        logger.error(f"[{exchange_name}/{market_type}] Failed to load markets: {e}")
        return set()


async def get_24h_volume(exchange_name: str, symbol: str, market_type: str = "spot") -> float:
    """Return 24h volume in USDT. Returns 0 on failure."""
    try:
        ex = await get_exchange(exchange_name, market_type)
        ticker = await ex.fetch_ticker(symbol)
        return float(ticker.get("quoteVolume") or ticker.get("baseVolume", 0))
    except Exception:
        return 0.0


async def fetch_ticker_price(exchange_name: str, symbol: str, market_type: str = "spot") -> Optional[float]:
    """Fetch the latest price for a single symbol. Used by outcome tracker."""
    try:
        ex = await get_exchange(exchange_name, market_type)
        ticker = await ex.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if price:
            return float(price)
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if bid and ask:
            return (float(bid) + float(ask)) / 2.0
    except Exception as e:
        logger.debug(f"[{exchange_name}] Ticker fetch failed {symbol}: {e}")
    return None


async def fetch_multi_timeframe(
    exchange_name: str,
    symbol: str,
    timeframes: list[str],
    market_type: str = "spot",
) -> dict[str, pd.DataFrame]:
    """Fetch multiple timeframes concurrently. Returns {timeframe: DataFrame}."""
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
