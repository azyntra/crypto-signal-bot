"""
scanner.py — Signal scanning orchestrator (v3).

vs v2:
  - Fully concurrent (semaphore-bounded) — a full scan finishes in well
    under a minute instead of overrunning its own interval.
  - Volume filter via ONE bulk fetch_tickers call.
  - Regime gate: per-coin 4h regime decides which strategies may fire.
  - Global guards: BTC regime/shock filter, news guard, loss-streak
    cooldown, hourly rate limit, max-open-signal caps.
  - Cross-exchange dedup (symbol+direction, not per-exchange).
  - Charts attached to signals.
"""
import asyncio
from typing import Optional

from src.data.fetcher import (
    fetch_multi_timeframe, get_exchange_symbols, fetch_bulk_volumes,
    fetch_funding_rate,
)
from src.data.coin_universe import fetch_top_coins, build_pairs
from src.analysis.indicators import compute_indicators
from src.analysis.strategies import evaluate
from src.analysis.regime import classify_regime, get_btc_regime, btc_blocks_direction, news_guard_active
from src.analysis.adaptive import get_confidence_multiplier, is_on_loss_cooldown
from src.analysis.ai_filter import review_signal
from src.analysis.ml_predictor import predict_win_probability
from src.analysis.sentiment import get_fear_greed_index, apply_sentiment_bias
from src.signals.validator import validate_and_build
from src.signals.formatter import format_signal
from src.signals.charting import render_signal_chart
from src.delivery.telegram_bot import send_signal, send_signal_with_chart, send_admin
from src.database.db_logger import (
    save_signal, is_duplicate, count_open_signals, count_signals_last_hour,
)
from config.settings import (
    INTRADAY_TIMEFRAMES, SWING_TIMEFRAMES, MIN_VOLUME_USDT, MIN_CONFIDENCE,
    SCAN_EXCHANGES, MARKET_TYPE, SCAN_CONCURRENCY,
    MAX_SIGNALS_PER_HOUR, MAX_OPEN_SIGNALS, MAX_OPEN_PER_SYMBOL,
    DEDUP_WINDOW_MIN, FUNDING_ENABLED, FUNDING_EXTREME, FUNDING_PENALTY,
    ML_MIN_WIN_PROB, SEND_CHART_IMAGES,
)
from config.logger import get_logger

logger = get_logger(__name__)

_admin_alerted_cooldown = False


# ── Pair selection ────────────────────────────────────────────────────────────

async def _get_pairs(exchange: str) -> list[str]:
    top_coins = fetch_top_coins()
    ex_symbols, volumes = await asyncio.gather(
        get_exchange_symbols(exchange, MARKET_TYPE),
        fetch_bulk_volumes(exchange, MARKET_TYPE),
    )
    pairs = build_pairs(ex_symbols, top_coins)
    filtered = [p for p in pairs if volumes.get(p, 0) >= MIN_VOLUME_USDT]
    logger.info(f"[{exchange}] {len(filtered)} pairs pass volume filter (of {len(pairs)})")
    return filtered


# ── Global pre-scan guards ────────────────────────────────────────────────────

async def _global_guards(scan_name: str) -> Optional[dict]:
    """Returns btc regime dict if scanning may proceed, else None."""
    global _admin_alerted_cooldown

    event = news_guard_active()
    if event:
        logger.info(f"{scan_name} skipped: news guard active ({event})")
        return None

    if is_on_loss_cooldown():
        logger.info(f"{scan_name} skipped: loss-streak cooldown")
        if not _admin_alerted_cooldown:
            _admin_alerted_cooldown = True
            await send_admin("⚠️ <b>Scanner paused</b>\nConsecutive-loss cooldown active. "
                             "Will resume automatically.")
        return None
    _admin_alerted_cooldown = False

    btc = await get_btc_regime()
    if btc.get("shock"):
        logger.info(f"{scan_name} skipped: BTC shock circuit breaker")
        return None
    return btc


# ── Per-symbol processing ─────────────────────────────────────────────────────

async def _process_symbol(exchange: str, symbol: str, style: str, btc: dict):
    tfs = INTRADAY_TIMEFRAMES if style == "intraday" else SWING_TIMEFRAMES
    entry_tf, htf_tf, regime_tf = tfs[0], tfs[1], tfs[2] if len(tfs) > 2 else tfs[1]

    data = await fetch_multi_timeframe(exchange, symbol, tfs, MARKET_TYPE)
    if entry_tf not in data or regime_tf not in data:
        return

    ind_entry = compute_indicators(data[entry_tf])
    ind_htf = compute_indicators(data.get(htf_tf))
    ind_regime = compute_indicators(data.get(regime_tf)) if regime_tf != htf_tf else ind_htf
    if not ind_entry or not ind_regime:
        return

    # 1. Regime gate
    regime = classify_regime(ind_regime, ind_htf)
    if regime == "choppy":
        return

    # 2. Strategy evaluation (hard gates inside)
    cand = evaluate(ind_entry, ind_htf, regime)
    if not cand:
        return

    # 3. BTC filter
    block = btc_blocks_direction(btc, cand["direction"])
    if block:
        logger.debug(f"{symbol} {cand['direction']} blocked: {block}")
        return

    # 4. Build signal (SL/TP/entry zone/R:R)
    signal = validate_and_build(cand, style)
    if not signal:
        return

    # 5. Cross-exchange dedup + caps
    if is_duplicate(symbol, signal["direction"], style, DEDUP_WINDOW_MIN.get(style, 120)):
        return
    if count_open_signals() >= MAX_OPEN_SIGNALS:
        logger.debug("Max open signals reached")
        return
    if count_open_signals(symbol) >= MAX_OPEN_PER_SYMBOL:
        return
    if count_signals_last_hour() >= MAX_SIGNALS_PER_HOUR:
        logger.debug("Hourly signal rate limit reached")
        return

    # 6. Funding-rate crowding penalty (futures)
    if FUNDING_ENABLED:
        rate = await fetch_funding_rate(exchange, symbol)
        if rate is not None:
            signal["funding_rate"] = rate
            crowded_long = rate >= FUNDING_EXTREME and signal["direction"] == "LONG"
            crowded_short = rate <= -FUNDING_EXTREME and signal["direction"] == "SHORT"
            if crowded_long or crowded_short:
                signal["confidence"] -= FUNDING_PENALTY
                signal["reasons"].append(f"⚠ Crowded funding ({rate*100:.3f}%) — confidence reduced")

    # 7. ML predictor
    ml_prob = predict_win_probability(cand.get("indicators", {}), signal["direction"], style)
    signal["ml_win_prob"] = ml_prob
    if ml_prob < ML_MIN_WIN_PROB:
        logger.info(f"{symbol} dropped: ML win prob {ml_prob:.2f} < {ML_MIN_WIN_PROB}")
        return

    # 8. Sentiment bias
    sentiment = get_fear_greed_index()
    signal["confidence"] = apply_sentiment_bias(signal["confidence"], signal["direction"], sentiment)
    signal["sentiment"] = sentiment

    # 9. Adaptive multiplier (win-rate feedback)
    mult = get_confidence_multiplier(signal["direction"], style, signal.get("strategy"))
    signal["confidence"] = int(signal["confidence"] * mult)

    # 10. AI review (async, non-blocking) — final quality gate
    ai_review = await review_signal(cand, symbol, exchange, style, MARKET_TYPE,
                                    sentiment, regime, btc.get("regime"))
    if ai_review.get("action") == "REJECT":
        logger.info(f"{symbol} {signal['direction']} rejected by AI: {ai_review.get('reasoning')}")
        return
    signal["confidence"] = min(signal["confidence"], ai_review.get("adjusted_confidence", signal["confidence"]))
    signal["ai_reasoning"] = ai_review.get("reasoning", "")

    # 11. Final confidence gate
    if signal["confidence"] < MIN_CONFIDENCE:
        logger.debug(f"{symbol} dropped: final confidence {signal['confidence']} < {MIN_CONFIDENCE}")
        return

    # 12. Publish (with chart if enabled)
    text = format_signal(signal, symbol, exchange, style, entry_tf, MARKET_TYPE, regime)
    chart_png = None
    if SEND_CHART_IMAGES:
        chart_png = render_signal_chart(data[entry_tf], signal, symbol, entry_tf)
    if chart_png:
        msg_id = await send_signal_with_chart(text, chart_png)
    else:
        msg_id = await send_signal(text)

    save_signal(signal, symbol, exchange, MARKET_TYPE, style, entry_tf,
                msg_id, ai_review, regime, btc.get("regime"))
    logger.info(f"✅ {style} signal: {symbol} {signal['direction']} "
                f"[{signal['strategy']}] conf={signal['confidence']}%")


# ── Scan entry points ─────────────────────────────────────────────────────────

async def _run_scan(style: str):
    name = f"{style.upper()} SCAN"
    btc = await _global_guards(name)
    if btc is None:
        return

    logger.info(f"═══ {name} started (BTC regime: {btc.get('regime')}) ═══")
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def worker(exchange, symbol):
        async with sem:
            try:
                await _process_symbol(exchange, symbol, style, btc)
            except Exception as e:
                logger.debug(f"{style} error {symbol}: {e}")

    for exchange in SCAN_EXCHANGES:
        try:
            pairs = await _get_pairs(exchange)
            await asyncio.gather(*(worker(exchange, s) for s in pairs))
        except Exception as e:
            logger.error(f"{name} failed [{exchange}]: {e}")

    logger.info(f"═══ {name} complete ═══")


async def run_intraday_scan():
    await _run_scan("intraday")


async def run_swing_scan():
    await _run_scan("swing")
