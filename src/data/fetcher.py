"""
fetcher.py — Async multi-exchange market data layer (v3).

Key changes vs v2:
  - Bulk 24h volumes via ONE fetch_tickers() call per exchange instead of
    one fetch_ticker() per pair (v2 made 400+ REST calls per scan).
  - Short-lived OHLCV cache so intraday + swing scans don't re-download
    the same candles.
  - Funding rate fetch for futures crowding analysis.
  - Paginated historical OHLCV for the backtester.
"""
import asyncio
import time
from typing import Optional

import ccxt.async_support as ccxt
import pandas as pd

from config.logger import get_logger
from config.settings import EXCHANGE_CREDENTIALS, CANDLE_LIMIT, QUOTE_CURRENCY

logger = get_logger(__name__)

_exchanges: dict[str, ccxt.Exchange] = {}
_ohlcv_cache: dict[tuple, tuple[float, pd.DataFrame]] = {}

# seconds one closed candle stays valid per timeframe
_TF_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


def _make_exchange(name: str, market_type: str) -> ccxt.Exchange:
    creds = dict(EXCHANGE_CREDENTIALS.get(name, {}))
    opts = {
        "binance": {"defaultType": "future"},
        "bybit":   {"defaultType": "linear"},
    }
    creds["options"] = opts.get(name, {"defaultType": "future"}) if market_type == "futures" \
        else {"defaultType": "spot"}
    creds["enableRateLimit"] = True
    cls = getattr(ccxt, name, None)
    if cls is None:
        raise ValueError(f"Unknown exchange: {name}")
    ex = cls(creds)
    ex.verbose = False
    return ex


async def get_exchange(name: str, market_type: str = "futures") -> ccxt.Exchange:
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


# ── OHLCV ─────────────────────────────────────────────────────────────────────

async def fetch_ohlcv(
    exchange_name: str, symbol: str, timeframe: str,
    market_type: str = "futures", limit: int = CANDLE_LIMIT,
    use_cache: bool = True,
) -> Optional[pd.DataFrame]:
    """Fetch OHLCV candles. Cached until the current candle closes."""
    key = (exchange_name, symbol, timeframe, market_type, limit)
    now = time.time()
    tf_sec = _TF_SECONDS.get(timeframe, 900)

    if use_cache and key in _ohlcv_cache:
        fetched_at, df = _ohlcv_cache[key]
        # cache valid while we're still inside the same candle
        if int(fetched_at // tf_sec) == int(now // tf_sec):
            return df

    try:
        ex = await get_exchange(exchange_name, market_type)
        raw = await ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not raw or len(raw) < 60:
            return None
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)
        if use_cache:
            _ohlcv_cache[key] = (now, df)
            if len(_ohlcv_cache) > 800:   # bound memory
                oldest = sorted(_ohlcv_cache.items(), key=lambda kv: kv[1][0])[:200]
                for k, _ in oldest:
                    _ohlcv_cache.pop(k, None)
        return df
    except ccxt.NetworkError as e:
        logger.warning(f"[{exchange_name}] Network error {symbol} {timeframe}: {e}")
    except ccxt.ExchangeError as e:
        logger.debug(f"[{exchange_name}] Exchange error {symbol} {timeframe}: {e}")
    except Exception as e:
        logger.error(f"[{exchange_name}] Unexpected error {symbol} {timeframe}: {e}")
    return None


async def fetch_multi_timeframe(
    exchange_name: str, symbol: str, timeframes: list[str],
    market_type: str = "futures",
) -> dict[str, pd.DataFrame]:
    tasks = [fetch_ohlcv(exchange_name, symbol, tf, market_type) for tf in timeframes]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {tf: r for tf, r in zip(timeframes, results) if isinstance(r, pd.DataFrame)}


async def fetch_ohlcv_history(
    exchange_name: str, symbol: str, timeframe: str,
    days: int, market_type: str = "futures",
) -> Optional[pd.DataFrame]:
    """Paginated historical OHLCV for backtesting."""
    try:
        ex = await get_exchange(exchange_name, market_type)
        tf_sec = _TF_SECONDS.get(timeframe, 900)
        since = int((time.time() - days * 86400) * 1000)
        all_rows = []
        while True:
            batch = await ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
            if not batch:
                break
            all_rows.extend(batch)
            if len(batch) < 1000:
                break
            since = batch[-1][0] + tf_sec * 1000
            if len(all_rows) > 60_000:
                break
            await asyncio.sleep(0.15)
        if len(all_rows) < 100:
            return None
        df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.drop_duplicates(subset="timestamp")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        return df.astype(float)
    except Exception as e:
        logger.error(f"[{exchange_name}] History fetch failed {symbol} {timeframe}: {e}")
        return None


# ── Symbols / tickers ─────────────────────────────────────────────────────────

async def get_exchange_symbols(exchange_name: str, market_type: str = "futures") -> set[str]:
    try:
        ex = await get_exchange(exchange_name, market_type)
        markets = await ex.load_markets()
        return {
            m["symbol"].split(":")[0]  # normalize BTC/USDT:USDT → BTC/USDT
            for m in markets.values()
            if m.get("quote") == QUOTE_CURRENCY and m.get("active", True)
            and (market_type != "futures" or m.get("swap") or m.get("future"))
        }
    except Exception as e:
        logger.error(f"[{exchange_name}/{market_type}] Failed to load markets: {e}")
        return set()


async def fetch_bulk_volumes(exchange_name: str, market_type: str = "futures") -> dict[str, float]:
    """One fetch_tickers() call → {symbol: 24h quote volume}."""
    try:
        ex = await get_exchange(exchange_name, market_type)
        tickers = await ex.fetch_tickers()
        out = {}
        for sym, t in tickers.items():
            base_sym = sym.split(":")[0]
            vol = t.get("quoteVolume") or 0
            if not vol and t.get("baseVolume") and t.get("last"):
                vol = t["baseVolume"] * t["last"]
            out[base_sym] = float(vol or 0)
        return out
    except Exception as e:
        logger.error(f"[{exchange_name}] Bulk ticker fetch failed: {e}")
        return {}


async def fetch_ticker_price(exchange_name: str, symbol: str, market_type: str = "futures") -> Optional[float]:
    try:
        ex = await get_exchange(exchange_name, market_type)
        t = await ex.fetch_ticker(symbol)
        price = t.get("last") or t.get("close")
        if price:
            return float(price)
        if t.get("bid") and t.get("ask"):
            return (float(t["bid"]) + float(t["ask"])) / 2.0
    except Exception as e:
        logger.debug(f"[{exchange_name}] Ticker fetch failed {symbol}: {e}")
    return None


async def fetch_funding_rate(exchange_name: str, symbol: str) -> Optional[float]:
    """Current funding rate for a futures symbol (e.g. 0.0001 = 0.01%)."""
    try:
        ex = await get_exchange(exchange_name, "futures")
        fr = await ex.fetch_funding_rate(symbol)
        rate = fr.get("fundingRate")
        return float(rate) if rate is not None else None
    except Exception as e:
        logger.debug(f"[{exchange_name}] Funding rate failed {symbol}: {e}")
        return None
