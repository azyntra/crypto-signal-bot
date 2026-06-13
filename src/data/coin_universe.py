"""
coin_universe.py — Fetches the current top-N coins by market cap from CoinGecko
and maps them to tradable USDT pairs across exchanges.
Refreshed on a schedule so the bot always tracks the hottest markets.
"""
import asyncio
import time
from typing import Set

import requests
from config.logger import get_logger
from config.settings import TOP_N_COINS, QUOTE_CURRENCY, MIN_VOLUME_USDT

logger = get_logger(__name__)

# CoinGecko free API — no key needed
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"

# Simple symbol blocklist (stablecoins, wrapped tokens that don't give real signals)
BLOCKLIST = {
    "USDT", "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP",
    "WBTC", "WETH", "STETH", "CBETH", "RETH",
    "BTTC",
}

_cache: dict = {"symbols": set(), "updated_at": 0}
CACHE_TTL = 1800  # 30 min


def fetch_top_coins(n: int = TOP_N_COINS) -> Set[str]:
    """Return a set of base-currency symbols like {'BTC', 'ETH', 'SOL', ...}"""
    now = time.time()
    if _cache["symbols"] and (now - _cache["updated_at"]) < CACHE_TTL:
        return _cache["symbols"]

    symbols: Set[str] = set()
    try:
        per_page = min(n, 250)
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": 1,
            "sparkline": False,
        }
        resp = requests.get(COINGECKO_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        for coin in data:
            sym = coin.get("symbol", "").upper()
            # Basic volume filter at discovery stage
            vol = coin.get("total_volume", 0) or 0
            if sym and sym not in BLOCKLIST and vol >= MIN_VOLUME_USDT:
                symbols.add(sym)

        _cache["symbols"] = symbols
        _cache["updated_at"] = now
        logger.info(f"Top coins refreshed — {len(symbols)} coins loaded from CoinGecko")
    except Exception as exc:
        logger.warning(f"CoinGecko fetch failed: {exc}. Using cached list.")
        if _cache["symbols"]:
            return _cache["symbols"]
        # Hard fallback — top 30 well-known coins
        symbols = {
            "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","LINK","DOT",
            "MATIC","UNI","LTC","BCH","ATOM","APT","OP","ARB","SUI","NEAR",
            "INJ","FIL","GRT","SAND","MANA","AXS","AAVE","SNX","COMP","MKR",
        }
        _cache["symbols"] = symbols

    return _cache["symbols"]


def build_pairs(exchange_symbols: Set[str], top_coins: Set[str]) -> list[str]:
    """
    Intersect CoinGecko top coins with what's actually listed on the exchange.
    Returns pairs like ['BTC/USDT', 'ETH/USDT', ...]
    """
    pairs = []
    for sym in top_coins:
        candidate = f"{sym}/{QUOTE_CURRENCY}"
        if candidate in exchange_symbols:
            pairs.append(candidate)
    pairs.sort()
    return pairs
