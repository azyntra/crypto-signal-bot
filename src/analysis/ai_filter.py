"""
ai_filter.py — Gemini AI signal review + daily market brief (v3).

Fixes vs v2:
  - The Gemini SDK call is synchronous; v2 called it directly inside the
    async scan loop, blocking the entire event loop (including the outcome
    tracker) for seconds per signal. v3 wraps it in asyncio.to_thread.
  - Prompt now includes regime, strategy, and BTC context.
  - AI can only lower or confirm confidence — never inflate it.
  - New: daily AI market brief for the channel, and on-demand /ai analysis.
"""
import asyncio
import json
import time
from typing import Optional

from config.settings import GEMINI_API_KEY, AI_FILTER_ENABLED, AI_MODEL
from config.logger import get_logger

logger = get_logger(__name__)

_review_cache: dict = {}
CACHE_TTL = 600

SYSTEM_PROMPT = """You are a strict, skeptical crypto trading risk manager reviewing signals from an automated scanner before publication. Most signals should pass — the scanner already applies hard gates — but you exist to catch what rules miss.

REJECT when:
- The stated strategy contradicts the indicator picture (e.g. "trend pullback" but ADX weak and structure broken)
- The trade fights the BTC regime or the coin's own 4h regime
- Volume/money-flow contradicts the direction
- The setup depends on a single indicator with no confluence

APPROVE otherwise. You may LOWER adjusted_confidence for marginal setups. Never raise it above the scanner's confidence.

Respond with valid JSON only."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["APPROVE", "REJECT"]},
        "adjusted_confidence": {"type": "integer"},
        "reasoning": {"type": "string"},
        "risk_notes": {"type": "string"},
    },
    "required": ["action", "adjusted_confidence", "reasoning", "risk_notes"],
}


def _sync_generate(prompt: str, system: str, schema: Optional[dict] = None) -> str:
    """Blocking Gemini call — always run via asyncio.to_thread."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_API_KEY)
    cfg = dict(system_instruction=system, temperature=0.15)
    if schema:
        cfg["response_mime_type"] = "application/json"
        cfg["response_schema"] = schema
    resp = client.models.generate_content(
        model=AI_MODEL, contents=prompt,
        config=types.GenerateContentConfig(**cfg),
    )
    return resp.text.strip()


def _build_prompt(cand, symbol, exchange, style, market_type, sentiment, regime, btc_regime):
    ind = cand.get("indicators", {})
    def f(key, spec=".1f", default="N/A"):
        v = ind.get(key)
        return format(v, spec) if isinstance(v, (int, float)) else default

    return f"""Review this {style.upper()} signal:

SIGNAL: {symbol} {cand.get('direction')} on {exchange.upper()} ({market_type})
STRATEGY: {cand.get('strategy')}
SCANNER CONFIDENCE: {cand.get('confidence')}%
TRIGGERS: {', '.join(cand.get('reasons', []))}

REGIME: coin 4h = {regime} | BTC = {btc_regime}

INDICATORS (entry TF, closed candles):
RSI {f('rsi')} | ADX {f('adx', '.0f')} (DI+ {f('di_pos', '.0f')} / DI- {f('di_neg', '.0f')})
MACD hist {f('macd_hist', '.6f')} rising={ind.get('macd_hist_rising')}
EMA stack: bull={ind.get('ema_bull')} bear={ind.get('ema_bear')} | above EMA200: {ind.get('above_200')} (slope {f('ema200_slope', '.3f')}%)
SuperTrend: {'bull' if ind.get('supertrend_dir') == 1 else 'bear'}
BB %B {f('bb_pct', '.2f')} | BBW percentile {f('bbw_pctile', '.0f')}
Vol ratio {f('vol_ratio')}x | MFI {f('mfi', '.0f')} | CMF {f('cmf', '.3f')} | OBV rising: {ind.get('obv_rising')}
Structure: bull={ind.get('structure_bull')} bear={ind.get('structure_bear')} | above VWAP: {ind.get('above_vwap')}
ATR {f('atr_pct', '.2f')}% | Candle: engulf(B/S)={ind.get('bull_engulf')}/{ind.get('bear_engulf')} pin(B/S)={ind.get('bull_pin')}/{ind.get('bear_pin')}
Nearest R: {ind.get('nearest_resistance')} | Nearest S: {ind.get('nearest_support')}
{f'Fear&Greed: {sentiment.get("value")} ({sentiment.get("label")})' if sentiment else ''}

APPROVE or REJECT? JSON only."""


async def review_signal(cand: dict, symbol: str, exchange: str, style: str,
                        market_type: str, sentiment: Optional[dict] = None,
                        regime: str = "?", btc_regime: str = "?") -> dict:
    original_conf = cand.get("confidence", 0)
    fallback = {"action": "APPROVE", "adjusted_confidence": original_conf,
                "reasoning": "AI filter bypassed", "risk_notes": ""}

    if not AI_FILTER_ENABLED or not GEMINI_API_KEY:
        return fallback

    key = f"{symbol}:{cand.get('direction')}:{style}"
    now = time.time()
    if key in _review_cache and now - _review_cache[key][0] < CACHE_TTL:
        return _review_cache[key][1]

    try:
        prompt = _build_prompt(cand, symbol, exchange, style, market_type,
                               sentiment, regime, btc_regime)
        text = await asyncio.wait_for(
            asyncio.to_thread(_sync_generate, prompt, SYSTEM_PROMPT, REVIEW_SCHEMA),
            timeout=25,
        )
        result = json.loads(text)
        action = result.get("action", "APPROVE")
        if action not in ("APPROVE", "REJECT"):
            action = "APPROVE"
        adj = int(result.get("adjusted_confidence", original_conf))
        adj = max(0, min(adj, original_conf))   # AI can only lower, never inflate

        review = {"action": action, "adjusted_confidence": adj,
                  "reasoning": result.get("reasoning", ""),
                  "risk_notes": result.get("risk_notes", "")}
        _review_cache[key] = (now, review)
        for k in [k for k, (t, _) in _review_cache.items() if now - t > CACHE_TTL]:
            del _review_cache[k]

        logger.info(f"AI {action}: {symbol} {cand.get('direction')} "
                    f"conf {original_conf}→{adj} | {review['reasoning']}")
        return review
    except Exception as e:
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            logger.debug("AI filter quota exhausted — pass-through")
        else:
            logger.warning(f"AI filter error: {e} — pass-through")
        return fallback


# ── Daily market brief (new v3 feature) ───────────────────────────────────────

BRIEF_SYSTEM = """You are a concise crypto market analyst. Write a short daily
outlook for a trading signal channel. Max 120 words. Use plain language,
no financial advice disclaimer needed (channel adds its own), no markdown
headers. Cover: BTC state, overall regime, what the bot will favor today
(longs/shorts/nothing), one key risk. You may use these emoji: 📊 ⚠️ 🟢 🔴"""


async def generate_daily_brief(context: dict) -> Optional[str]:
    """context: {btc_regime, btc_change_24h, fear_greed, top_movers, stats}"""
    if not GEMINI_API_KEY:
        return None
    try:
        prompt = f"""Market data for today's brief:
BTC regime: {context.get('btc_regime')}
BTC 24h change: {context.get('btc_change_24h', 'N/A')}%
Fear & Greed: {context.get('fear_greed')}
Top movers (24h): {context.get('top_movers')}
Bot performance last 7d: {context.get('stats')}

Write the daily outlook."""
        text = await asyncio.wait_for(
            asyncio.to_thread(_sync_generate, prompt, BRIEF_SYSTEM),
            timeout=30,
        )
        return text
    except Exception as e:
        logger.warning(f"Daily brief error: {e}")
        return None


ANALYZE_SYSTEM = """You are a crypto technical analyst. Given indicator data
for a coin, give a concise structured read: trend, momentum, key levels,
and bias (bullish/bearish/neutral with rough probability). Max 150 words.
Plain text, no markdown headers."""


async def analyze_symbol_ai(symbol: str, ind_1h: dict, ind_4h: dict, regime: str) -> Optional[str]:
    """On-demand /ai command analysis."""
    if not GEMINI_API_KEY:
        return None
    try:
        def brief(ind):
            if not ind:
                return "no data"
            keys = ("price", "rsi", "adx", "macd_hist", "ema_bull", "ema_bear",
                    "above_200", "supertrend_dir", "bb_pct", "vol_ratio", "mfi",
                    "cmf", "structure_bull", "structure_bear",
                    "nearest_resistance", "nearest_support")
            return {k: ind.get(k) for k in keys}

        prompt = (f"{symbol} — regime: {regime}\n"
                  f"1h: {json.dumps(brief(ind_1h), default=str)}\n"
                  f"4h: {json.dumps(brief(ind_4h), default=str)}\n\nAnalysis:")
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_generate, prompt, ANALYZE_SYSTEM),
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"AI analyze error: {e}")
        return None
