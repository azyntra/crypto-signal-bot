"""
formatter.py — Telegram message formatting (v3).
"""
from typing import Optional


def _fmt(v: Optional[float]) -> str:
    if v is None: return "N/A"
    if v >= 1000: return f"{v:,.2f}"
    if v >= 1:    return f"{v:.4f}"
    if v >= 0.01: return f"{v:.5f}"
    return f"{v:.8f}"


STRATEGY_LABELS = {
    "trend_pullback":   "📈 Trend Pullback",
    "range_fade":       "🔁 Range Reversal",
    "squeeze_breakout": "💥 Squeeze Breakout",
}

REGIME_LABELS = {
    "trend_up": "Uptrend", "trend_down": "Downtrend",
    "range": "Range", "choppy": "Choppy",
}


def format_signal(signal: dict, symbol: str, exchange: str, style: str,
                  timeframe: str, market_type: str, regime: str = None) -> str:
    d = signal["direction"]
    dir_emoji = "🟢" if d == "LONG" else "🔴"
    style_emoji = "⚡" if style == "intraday" else "📈"
    strat = STRATEGY_LABELS.get(signal.get("strategy"), signal.get("strategy", ""))

    conf = signal["confidence"]
    conf_bar = "█" * round(conf / 100 * 8) + "░" * (8 - round(conf / 100 * 8))

    lines = [
        "─" * 30,
        f"{dir_emoji} <b>{d} — {symbol}</b>",
        "─" * 30,
        f"{style_emoji} {style.upper()} · {timeframe} · {exchange.upper()} {market_type.upper()}",
        f"{strat}" + (f"  ·  Regime: {REGIME_LABELS.get(regime, regime)}" if regime else ""),
        "",
        f"🎯 Confidence: {conf_bar} <b>{conf}%</b>",
        "",
        f"📍 Entry zone: <b>${_fmt(signal['entry_low'])} – ${_fmt(signal['entry_high'])}</b>",
        "   (wait for price to enter the zone — do not chase)",
        "",
        f"🎯 TP1: ${_fmt(signal['tp1'])}  (+{signal['tp1_pct']}%)",
        f"🎯 TP2: ${_fmt(signal['tp2'])}  (+{signal['tp2_pct']}%)",
        f"🎯 TP3: ${_fmt(signal['tp3'])}  (+{signal['tp3_pct']}%)",
        f"🛡 SL:  ${_fmt(signal['stop_loss'])}  (-{signal['risk_pct']}%)",
        f"⚖️ R:R at TP2: <b>{signal['rr_ratio']}</b>",
        "",
        "💡 Plan: close ⅓ at each TP · SL → breakeven after TP1",
    ]
    if signal.get("risk_note"):
        lines.append(f"📐 {signal['risk_note']}")

    reasons = signal.get("reasons", [])
    if reasons:
        lines += ["", "<b>Why:</b>"] + [f" • {r}" for r in reasons[:6]]

    if signal.get("ai_reasoning"):
        lines += ["", f"🤖 AI: <i>{signal['ai_reasoning']}</i>"]

    sent = signal.get("sentiment")
    if sent:
        lines.append(f"😨 Fear & Greed: {sent.get('value')} ({sent.get('label')})")
    if signal.get("funding_rate") is not None:
        lines.append(f"💸 Funding: {signal['funding_rate']*100:+.4f}%")

    lines += [
        "─" * 30,
        f"#{symbol.replace('/', '')} #{d} #{style.upper()} #{(signal.get('strategy') or '').upper()}",
        "⚠️ <i>Not financial advice. Risk max 1-2% per trade.</i>",
    ]
    return "\n".join(lines)


def format_startup_message(version: str) -> str:
    return (
        "─" * 30 + "\n"
        f"🤖 <b>Crypto Signal Bot v{version} online</b>\n"
        + "─" * 30 + "\n\n"
        "⚡ Intraday scanner: 15m entries (every 15 min)\n"
        "📈 Swing scanner: 1h entries (every 60 min)\n"
        "🧭 Regime-gated strategies + BTC filter\n"
        "🔍 Candle-accurate outcome tracking\n"
        "📊 All results posted — wins AND losses\n"
    )


def format_error_alert(context: str, error: str) -> str:
    return (f"🚨 <b>Bot error</b>\n\n"
            f"Context: {context}\n"
            f"<code>{error[:500]}</code>")
