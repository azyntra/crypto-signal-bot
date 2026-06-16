"""
coin_universe.py — Fetches the current top-N coins by market cap from CoinGecko
and maps them to tradable USDT pairs across exchanges.
v2.0: Smart coin selection — scores coins by tradability (volume, trend strength,
      price action) and prioritizes the best ones for scanning.
"""
import time
from typing import Set

import requests
from config.logger import get_logger
from config.settings import (
    TOP_N_COINS, QUOTE_CURRENCY, MIN_VOLUME_USDT,
    SMART_COIN_SELECTION, MAX_TRADABLE_COINS,
)

logger = get_logger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"

# Stablecoins, wrapped tokens, pegged assets that don't give real signals
BLOCKLIST = {
    # Major USD stablecoins
    "USDT", "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP",
    "USDE", "USDTB", "USD1", "PYUSD", "BFUSD", "EURC", "GHO",
    "FRAX", "CRVUSD", "LUSD", "SUSD", "MIM", "GUSD", "ALUSD",
    "EURS", "USDD", "USDJ", "USTC", "CUSD", "RSR", "FEI",
    "USDB", "ZUSD", "HUSD", "USDX", "USDQ", "UST", "USDN",
    "USDK", "TRIBE", "BEAN", "DOLA",
    # Wrapped / pegged tokens (track underlying, not tradeable)
    "WBTC", "WETH", "STETH", "CBETH", "RETH", "WSTETH", "METH",
    "CBBTC", "LSETH", "SWETH", "WBETH", "TBTC",
    # Low-quality / untradeable
    "BTTC",
}

_cache: dict = {"symbols": set(), "ranked": [], "updated_at": 0}
CACHE_TTL = 1800  # 30 min


def _score_coin(coin: dict) -> float:
    """
    Score a coin's tradability based on CoinGecko data.
    Higher score = better to trade.

    Factors:
    1. Volume rank (higher volume = more liquid = better)
    2. 24h price change magnitude (trending coins > flat coins)
    3. Market cap rank (top coins are more predictable)
    """
    score = 0.0

    # Volume: higher is better (log scale to compress range)
    vol = coin.get("total_volume", 0) or 0
    if vol > 100_000_000:    # > $100M
        score += 40
    elif vol > 50_000_000:
        score += 30
    elif vol > 10_000_000:
        score += 20
    elif vol > 1_000_000:
        score += 10

    # Price change: trending coins get bonus (magnitude, not direction)
    change_24h = abs(coin.get("price_change_percentage_24h", 0) or 0)
    if change_24h > 5:
        score += 30    # strong trend
    elif change_24h > 3:
        score += 20    # moderate trend
    elif change_24h > 1:
        score += 10    # some movement
    # < 1% change = flat/ranging, no bonus

    # Market cap rank: top coins are more predictable
    rank = coin.get("market_cap_rank", 999) or 999
    if rank <= 20:
        score += 30
    elif rank <= 50:
        score += 20
    elif rank <= 100:
        score += 10

    return score


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

        scored_coins = []
        for coin in data:
            sym = coin.get("symbol", "").upper()
            if sym and sym not in BLOCKLIST:
                symbols.add(sym)
                scored_coins.append((sym, _score_coin(coin)))

        # Sort by tradability score (descending)
        scored_coins.sort(key=lambda x: x[1], reverse=True)

        if SMART_COIN_SELECTION and len(scored_coins) > MAX_TRADABLE_COINS:
            # Only keep the most tradable coins
            top_symbols = {s for s, _ in scored_coins[:MAX_TRADABLE_COINS]}
            dropped = symbols - top_symbols
            if dropped:
                logger.info(f"Smart selection: dropped {len(dropped)} low-tradability coins: "
                            f"{', '.join(sorted(dropped)[:5])}...")
            symbols = top_symbols

        _cache["symbols"] = symbols
        _cache["ranked"] = scored_coins
        _cache["updated_at"] = now
        logger.info(f"Top coins refreshed — {len(symbols)} coins loaded from CoinGecko"
                    + (f" (smart-filtered from {len(data)})" if SMART_COIN_SELECTION else ""))
    except Exception as exc:
        logger.warning(f"CoinGecko fetch failed: {exc}. Using cached list.")
        if _cache["symbols"]:
            return _cache["symbols"]
        symbols = {
            "BTC","ETH","BNB","SOL","XRP","ADA","DOGE","AVAX","LINK","DOT",
            "MATIC","UNI","LTC","BCH","ATOM","APT","OP","ARB","SUI","NEAR",
            "INJ","FIL","GRT","SAND","MANA","AXS","AAVE","SNX","COMP","MKR",
        }
        _cache["symbols"] = symbols

    return _cache["symbols"]


def build_pairs(exchange_symbols: Set[str], top_coins: Set[str]) -> list[str]:
    pairs = []
    for sym in top_coins:
        candidate = f"{sym}/{QUOTE_CURRENCY}"
        if candidate in exchange_symbols:
            pairs.append(candidate)
    pairs.sort()
    return pairs
