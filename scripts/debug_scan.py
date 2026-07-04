"""
debug_scan.py — Dry-run one scan pass without sending Telegram messages.

Usage:
    PYTHONPATH=. python scripts/debug_scan.py [intraday|swing]

Prints regime + best strategy candidate per symbol so you can see WHY
signals are (not) firing.
"""
import asyncio
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data.fetcher import fetch_multi_timeframe, get_exchange_symbols, fetch_bulk_volumes, close_all
from src.data.coin_universe import fetch_top_coins, build_pairs
from src.analysis.indicators import compute_indicators
from src.analysis.strategies import evaluate
from src.analysis.regime import classify_regime, get_btc_regime
from src.signals.validator import validate_and_build
from config.settings import (
    INTRADAY_TIMEFRAMES, SWING_TIMEFRAMES, MARKET_TYPE,
    SCAN_EXCHANGES, MIN_VOLUME_USDT,
)


async def test(style: str = "intraday"):
    exchange = SCAN_EXCHANGES[0]
    tfs = INTRADAY_TIMEFRAMES if style == "intraday" else SWING_TIMEFRAMES

    btc = await get_btc_regime()
    print(f"BTC regime: {btc.get('regime')}  shock: {btc.get('shock')}\n")

    top_coins = fetch_top_coins()
    ex_symbols = await get_exchange_symbols(exchange, MARKET_TYPE)
    volumes = await fetch_bulk_volumes(exchange, MARKET_TYPE)
    pairs = [p for p in build_pairs(ex_symbols, top_coins)
             if volumes.get(p, 0) >= MIN_VOLUME_USDT]
    print(f"Scanning {len(pairs)} pairs on {exchange}/{MARKET_TYPE} ({style})...\n")

    candidates = 0
    for symbol in pairs[:40]:
        try:
            data = await fetch_multi_timeframe(exchange, symbol, tfs, MARKET_TYPE)
            if tfs[0] not in data:
                continue
            ind_entry = compute_indicators(data[tfs[0]])
            ind_htf = compute_indicators(data.get(tfs[1]))
            ind_regime = compute_indicators(data.get(tfs[2])) if len(tfs) > 2 else ind_htf
            if not ind_entry or not ind_regime:
                continue
            regime = classify_regime(ind_regime, ind_htf)
            cand = evaluate(ind_entry, ind_htf, regime)
            line = f"{symbol:<16} regime={regime:<11}"
            if cand:
                sig = validate_and_build(cand, style)
                ok = "✅ SIGNAL" if sig else "⚠ failed validation"
                line += f" {cand['direction']} {cand['strategy']} conf={cand['confidence']} {ok}"
                candidates += 1
            print(line)
        except Exception as e:
            print(f"{symbol:<16} error: {e}")

    print(f"\n{candidates} candidates found.")
    await close_all()


if __name__ == "__main__":
    style = sys.argv[1] if len(sys.argv) > 1 else "intraday"
    asyncio.run(test(style))
