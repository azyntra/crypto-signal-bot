"""
scanner.py — Orchestrates the full signal scanning loop.
For each exchange × symbol × timeframe → indicators → score → validate → send.
Runs scalping and swing scans on their own schedules.
"""
import asyncio
import random
from typing import Optional

from src.data.fetcher      import fetch_multi_timeframe, get_exchange_symbols
from src.data.coin_universe import fetch_top_coins, build_pairs
from src.analysis.indicators import compute_indicators
from src.analysis.scalping   import score_scalp
from src.analysis.swing      import score_swing
from src.signals.validator   import validate_and_build
from src.signals.formatter   import format_signal, format_summary_report, format_error_alert
from src.delivery.telegram_bot import send_signal, send_admin
from src.database.db_logger  import save_signal, is_duplicate, init_db

from config.settings import (
    SCALPING_TIMEFRAMES, SWING_TIMEFRAMES, MIN_VOLUME_USDT,
    SPOT_EXCHANGE,
)
from config.logger import get_logger

logger = get_logger(__name__)

# Exchanges to scan — public data only needed for signals (no API key required for read)
SCAN_EXCHANGES = ["binance", "bybit", "okx"]
MARKET_TYPES   = ["spot", "futures"]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_pairs(exchange: str, market_type: str) -> list[str]:
    """Get top-N valid pairs for an exchange."""
    top_coins = fetch_top_coins()
    ex_symbols = await get_exchange_symbols(exchange, market_type)
    pairs = build_pairs(ex_symbols, top_coins)
    logger.info(f"[{exchange}/{market_type}] {len(pairs)} pairs to scan")
    return pairs


async def _process_scalp(exchange: str, symbol: str, market_type: str):
    """Run scalping analysis for one symbol."""
    tfs = SCALPING_TIMEFRAMES  # ["1m", "5m", "15m"]
    data = await fetch_multi_timeframe(exchange, symbol, tfs, market_type)

    if "5m" not in data:
        return

    ind_5m  = compute_indicators(data.get("5m"))
    ind_15m = compute_indicators(data.get("15m"))

    if not ind_5m:
        return

    result = score_scalp(ind_fast=ind_5m, ind_mid=ind_15m)

    if not result["direction"]:
        return

    signal = validate_and_build(result, market_type)
    if not signal:
        return

    # Dedup: don't resend same direction in last 30 min
    if is_duplicate(symbol, exchange, signal["direction"], "scalp", window_minutes=30):
        logger.debug(f"Duplicate scalp signal skipped: {symbol} {signal['direction']}")
        return

    text = format_signal(signal, symbol, exchange, "scalp", "5m", market_type)
    msg_id = await send_signal(text)
    save_signal(signal, symbol, exchange, market_type, "scalp", "5m", msg_id)
    logger.info(f"✅ Scalp signal: {symbol} {signal['direction']} conf={signal['confidence']}%")


async def _process_swing(exchange: str, symbol: str, market_type: str):
    """Run swing analysis for one symbol."""
    tfs  = SWING_TIMEFRAMES  # ["1h", "4h", "1d"]
    data = await fetch_multi_timeframe(exchange, symbol, tfs, market_type)

    if "4h" not in data:
        return

    ind_4h = compute_indicators(data.get("4h"))
    ind_1d = compute_indicators(data.get("1d"))

    if not ind_4h:
        return

    result = score_swing(ind_base=ind_4h, ind_high=ind_1d)

    if not result["direction"]:
        return

    signal = validate_and_build(result, market_type)
    if not signal:
        return

    if is_duplicate(symbol, exchange, signal["direction"], "swing", window_minutes=240):
        logger.debug(f"Duplicate swing signal skipped: {symbol} {signal['direction']}")
        return

    text = format_signal(signal, symbol, exchange, "swing", "4h", market_type)
    msg_id = await send_signal(text)
    save_signal(signal, symbol, exchange, market_type, "swing", "4h", msg_id)
    logger.info(f"✅ Swing signal: {symbol} {signal['direction']} conf={signal['confidence']}%")


# ── Public scan entry points ───────────────────────────────────────────────────

async def run_scalp_scan():
    """Full scalping scan across all exchanges and pairs."""
    logger.info("═══ SCALP SCAN started ═══")
    total_signals = 0

    for exchange in SCAN_EXCHANGES:
        for market_type in MARKET_TYPES:
            try:
                pairs = await _get_pairs(exchange, market_type)
                # Shuffle so we don't always hit the same pairs first (rate limit fairness)
                random.shuffle(pairs)
                for symbol in pairs:
                    try:
                        await _process_scalp(exchange, symbol, market_type)
                        await asyncio.sleep(0.3)  # gentle rate limiting
                    except Exception as e:
                        logger.debug(f"Scalp error {symbol}: {e}")
            except Exception as e:
                logger.error(f"Scalp scan failed [{exchange}/{market_type}]: {e}")
                await send_admin(format_error_alert(f"scalp scan {exchange}", str(e)))

    logger.info("═══ SCALP SCAN complete ═══")


async def run_swing_scan():
    """Full swing scan across all exchanges and pairs."""
    logger.info("═══ SWING SCAN started ═══")

    for exchange in SCAN_EXCHANGES:
        for market_type in MARKET_TYPES:
            try:
                pairs = await _get_pairs(exchange, market_type)
                random.shuffle(pairs)
                for symbol in pairs:
                    try:
                        await _process_swing(exchange, symbol, market_type)
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.debug(f"Swing error {symbol}: {e}")
            except Exception as e:
                logger.error(f"Swing scan failed [{exchange}/{market_type}]: {e}")
                await send_admin(format_error_alert(f"swing scan {exchange}", str(e)))

    logger.info("═══ SWING SCAN complete ═══")
