"""
scanner.py — Orchestrates the full signal scanning loop.
v2.0: Added loss-streak cooldown, adaptive confidence multiplier,
      and direction blocking from win-rate feedback.
"""
import asyncio
import random
from typing import Optional

from src.data.fetcher      import fetch_multi_timeframe, get_exchange_symbols
from src.data.coin_universe import fetch_top_coins, build_pairs
from src.analysis.indicators import compute_indicators
from src.analysis.scalping   import score_scalp
from src.analysis.swing      import score_swing
from src.analysis.adaptive   import (
    get_confidence_multiplier, is_direction_blocked, is_on_loss_cooldown,
)
from src.analysis.ai_filter  import review_signal
from src.analysis.ml_predictor import predict_win_probability
from src.analysis.sentiment  import get_fear_greed_index, apply_sentiment_bias
from src.signals.validator   import validate_and_build
from src.signals.formatter   import format_signal, format_summary_report, format_error_alert
from src.delivery.telegram_bot import send_signal, send_admin
from src.database.db_logger  import save_signal, is_duplicate, init_db

from config.settings import (
    SCALPING_TIMEFRAMES, SWING_TIMEFRAMES, MIN_VOLUME_USDT,
    SPOT_EXCHANGE, MIN_CONFIDENCE_SCALP, MIN_CONFIDENCE_SWING,
)
from config.logger import get_logger

logger = get_logger(__name__)

SCAN_EXCHANGES = ["binance", "bybit"]
MARKET_TYPES   = ["spot", "futures"]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_pairs(exchange: str, market_type: str) -> list[str]:
    from src.data.fetcher import get_24h_volume
    top_coins = fetch_top_coins()
    ex_symbols = await get_exchange_symbols(exchange, market_type)
    pairs = build_pairs(ex_symbols, top_coins)
    
    # Filter by actual per-exchange volume
    filtered_pairs = []
    # Process sequentially or in small batches to avoid rate limits, but for simplicity we'll just gather
    # To be safe against rate limits, we use a semaphore
    sem = asyncio.Semaphore(10)
    async def check_vol(pair):
        async with sem:
            vol = await get_24h_volume(exchange, pair, market_type)
            return pair if vol >= MIN_VOLUME_USDT else None

    results = await asyncio.gather(*(check_vol(p) for p in pairs))
    filtered_pairs = [p for p in results if p]

    logger.info(f"[{exchange}/{market_type}] {len(filtered_pairs)} pairs to scan (filtered from {len(pairs)})")
    return filtered_pairs


async def _process_scalp(exchange: str, symbol: str, market_type: str):
    tfs = SCALPING_TIMEFRAMES
    data = await fetch_multi_timeframe(exchange, symbol, tfs, market_type)

    if "5m" not in data:
        return

    ind_1m  = compute_indicators(data.get("1m"))
    ind_5m  = compute_indicators(data.get("5m"))
    ind_15m = compute_indicators(data.get("15m"))

    if not ind_5m:
        return

    # Try 1m first
    result = score_scalp(ind_fast=ind_1m, ind_mid=ind_5m) if ind_1m else {"direction": None}
    if result.get("direction"):
        tf_str = "1m"
        dedup_window = 15
    else:
        # Fallback to 5m
        result = score_scalp(ind_fast=ind_5m, ind_mid=ind_15m)
        tf_str = "5m"
        dedup_window = 30

    if not result.get("direction"):
        return

    # ── Adaptive: check if direction is blocked ──────────────────────────────
    if is_direction_blocked(result["direction"], "scalp"):
        logger.debug(f"Scalp {result['direction']} blocked by adaptive (low win rate)")
        return

    signal = validate_and_build(result, market_type, style="scalp")
    if not signal:
        return

    # ── AI & ML Pipeline ─────────────────────────────────────────────────────
    sentiment = get_fear_greed_index()
    
    # 1. Self-Learning ML Predictor
    ml_prob = predict_win_probability(result.get("indicators", {}), signal["direction"], "scalp")
    signal["ml_win_prob"] = ml_prob
    if ml_prob < 0.40:
        logger.debug(f"Scalp {signal['direction']} dropped: low ML win prob {ml_prob:.2f}")
        return
        
    # 2. Gemini AI Filter
    ai_review = await review_signal(result, symbol, exchange, "scalp", market_type, sentiment)
    if ai_review.get("action") == "REJECT":
        logger.debug(f"Scalp {signal['direction']} REJECTED by AI: {ai_review.get('reasoning')}")
        return
    
    signal["confidence"] = ai_review.get("adjusted_confidence", signal["confidence"])
    signal["ai_reasoning"] = ai_review.get("reasoning", "")
    
    # 3. Sentiment Bias
    signal["confidence"] = apply_sentiment_bias(signal["confidence"], signal["direction"], sentiment)
    signal["sentiment"] = sentiment

    # ── Adaptive: apply confidence multiplier ────────────────────────────────
    mult = get_confidence_multiplier(signal["direction"], "scalp", exchange)
    adjusted_conf = int(signal["confidence"] * mult)
    if adjusted_conf < MIN_CONFIDENCE_SCALP:
        logger.debug(f"Scalp signal dropped: adaptive reduced conf {signal['confidence']}→{adjusted_conf}")
        return
    signal["confidence"] = adjusted_conf

    if is_duplicate(symbol, exchange, signal["direction"], "scalp", window_minutes=dedup_window):
        logger.debug(f"Duplicate scalp signal skipped: {symbol} {signal['direction']}")
        return

    text = format_signal(signal, symbol, exchange, "scalp", tf_str, market_type)
    msg_id = await send_signal(text)
    save_signal(signal, symbol, exchange, market_type, "scalp", tf_str, msg_id, ai_review)
    logger.info(f"✅ Scalp signal: {symbol} {signal['direction']} conf={signal['confidence']}%")


async def _process_swing(exchange: str, symbol: str, market_type: str):
    tfs  = SWING_TIMEFRAMES
    data = await fetch_multi_timeframe(exchange, symbol, tfs, market_type)

    if "4h" not in data:
        return

    ind_1h = compute_indicators(data.get("1h"))
    ind_4h = compute_indicators(data.get("4h"))
    ind_1d = compute_indicators(data.get("1d"))

    if not ind_4h:
        return

    # Try 1h first
    result = score_swing(ind_base=ind_1h, ind_high=ind_4h) if ind_1h else {"direction": None}
    if result.get("direction"):
        tf_str = "1h"
        dedup_window = 60
    else:
        # Fallback to 4h
        result = score_swing(ind_base=ind_4h, ind_high=ind_1d)
        tf_str = "4h"
        dedup_window = 240

    if not result.get("direction"):
        return

    # ── Adaptive: check if direction is blocked ──────────────────────────────
    if is_direction_blocked(result["direction"], "swing"):
        logger.debug(f"Swing {result['direction']} blocked by adaptive (low win rate)")
        return

    signal = validate_and_build(result, market_type, style="swing")
    if not signal:
        return

    # ── AI & ML Pipeline ─────────────────────────────────────────────────────
    sentiment = get_fear_greed_index()
    
    # 1. Self-Learning ML Predictor
    ml_prob = predict_win_probability(result.get("indicators", {}), signal["direction"], "swing")
    signal["ml_win_prob"] = ml_prob
    if ml_prob < 0.40:
        logger.debug(f"Swing {signal['direction']} dropped: low ML win prob {ml_prob:.2f}")
        return
        
    # 2. Gemini AI Filter
    ai_review = await review_signal(result, symbol, exchange, "swing", market_type, sentiment)
    if ai_review.get("action") == "REJECT":
        logger.debug(f"Swing {signal['direction']} REJECTED by AI: {ai_review.get('reasoning')}")
        return
    
    signal["confidence"] = ai_review.get("adjusted_confidence", signal["confidence"])
    signal["ai_reasoning"] = ai_review.get("reasoning", "")
    
    # 3. Sentiment Bias
    signal["confidence"] = apply_sentiment_bias(signal["confidence"], signal["direction"], sentiment)
    signal["sentiment"] = sentiment

    # ── Adaptive: apply confidence multiplier ────────────────────────────────
    mult = get_confidence_multiplier(signal["direction"], "swing", exchange)
    adjusted_conf = int(signal["confidence"] * mult)
    if adjusted_conf < MIN_CONFIDENCE_SWING:
        logger.debug(f"Swing signal dropped: adaptive reduced conf {signal['confidence']}→{adjusted_conf}")
        return
    signal["confidence"] = adjusted_conf

    if is_duplicate(symbol, exchange, signal["direction"], "swing", window_minutes=dedup_window):
        logger.debug(f"Duplicate swing signal skipped: {symbol} {signal['direction']}")
        return

    text = format_signal(signal, symbol, exchange, "swing", tf_str, market_type)
    msg_id = await send_signal(text)
    save_signal(signal, symbol, exchange, market_type, "swing", tf_str, msg_id, ai_review)
    logger.info(f"✅ Swing signal: {symbol} {signal['direction']} conf={signal['confidence']}%")


# ── Public scan entry points ───────────────────────────────────────────────────

async def run_scalp_scan():
    """Full scalping scan across all exchanges and pairs."""

    # ── Loss streak cooldown check ───────────────────────────────────────────
    if is_on_loss_cooldown():
        logger.info("═══ SCALP SCAN skipped: loss streak cooldown active ═══")
        await send_admin("⚠️ <b>Scanner paused</b>\n\n"
                         f"3+ consecutive losses detected.\n"
                         f"Cooldown active — scanner will resume automatically.")
        return

    logger.info("═══ SCALP SCAN started ═══")

    for exchange in SCAN_EXCHANGES:
        for market_type in MARKET_TYPES:
            try:
                pairs = await _get_pairs(exchange, market_type)
                random.shuffle(pairs)
                for symbol in pairs:
                    try:
                        await _process_scalp(exchange, symbol, market_type)
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.debug(f"Scalp error {symbol}: {e}")
            except Exception as e:
                logger.error(f"Scalp scan failed [{exchange}/{market_type}]: {e}")
                await send_admin(format_error_alert(f"scalp scan {exchange}", str(e)))

    logger.info("═══ SCALP SCAN complete ═══")


async def run_swing_scan():
    """Full swing scan across all exchanges and pairs."""

    # ── Loss streak cooldown check ───────────────────────────────────────────
    if is_on_loss_cooldown():
        logger.info("═══ SWING SCAN skipped: loss streak cooldown active ═══")
        return

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
