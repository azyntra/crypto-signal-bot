"""
formatter.py — Builds formatted Telegram messages for signals.
Uses HTML formatting (supported by Telegram Bot API).
"""
import html
from datetime import datetime, timezone
from config.logger import get_logger

logger = get_logger(__name__)

DIRECTION_EMOJI = {"LONG": "🟢", "SHORT": "🔴"}
STYLE_EMOJI     = {"scalp": "⚡", "swing": "📈"}
MARKET_EMOJI    = {"spot": "💱", "futures": "⚙️"}


def _e(value) -> str:
    """Escape any dynamic value for safe insertion into Telegram HTML."""
    return html.escape(str(value))


def format_signal(
    signal: dict,
    symbol: str,
    exchange: str,
    style: str,          # 'scalp' or 'swing'
    timeframe: str,
    market_type: str,    # 'spot' or 'futures'
) -> str:
    """
    Render a complete signal as a Telegram HTML message string.
    All dynamic values are html.escape()'d to prevent parse errors.
    """
    d       = signal["direction"]
    conf    = signal["confidence"]
    reasons = signal["reasons"]
    price   = signal["price"]

    # ── Header ────────────────────────────────────────────────────────────────
    dir_emoji   = DIRECTION_EMOJI.get(d, "⚪")
    style_label = "SCALP" if style == "scalp" else "SWING"
    market_label = market_type.upper()

    lines = [
        f"{'─' * 32}",
        f"🚨 <b>SIGNAL ALERT — {_e(d)}</b> {dir_emoji}",
        f"{'─' * 32}",
        "",
        f"📊 <b>{_e(symbol)}</b>  ·  {_e(exchange.upper())}  ·  {_e(market_label)}",
        f"{STYLE_EMOJI[style]} <b>{_e(style_label)}</b>  |  ⏱ Timeframe: <b>{_e(timeframe)}</b>",
        "",
    ]

    # ── Entry zone ────────────────────────────────────────────────────────────
    lines += [
        f"{'🟢' if d == 'LONG' else '🔴'} <b>ENTRY ZONE</b>",
        f"   ${_e(_fmt(signal['entry_low']))} – ${_e(_fmt(signal['entry_high']))}",
        "",
    ]

    # ── Take profits ──────────────────────────────────────────────────────────
    tp_sign = "+" if d == "LONG" else "-"
    lines += [
        f"🎯 <b>TAKE PROFITS</b>",
        f"   TP1 → <b>${_e(_fmt(signal['tp1']))}</b>  ({_e(tp_sign)}{_e(signal['tp1_pct'])}%)",
        f"   TP2 → <b>${_e(_fmt(signal['tp2']))}</b>  ({_e(tp_sign)}{_e(signal['tp2_pct'])}%)",
        f"   TP3 → <b>${_e(_fmt(signal['tp3']))}</b>  ({_e(tp_sign)}{_e(signal['tp3_pct'])}%)",
        "",
    ]

    # ── Stop loss ─────────────────────────────────────────────────────────────
    sl_sign = "-" if d == "LONG" else "+"
    lines += [
        f"🛡 <b>STOP LOSS</b>",
        f"   ${_e(_fmt(signal['stop_loss']))}  ({_e(sl_sign)}{_e(signal['risk_pct'])}%)",
        "",
    ]

    # ── R:R + Leverage ────────────────────────────────────────────────────────
    lines += [
        f"⚖️ <b>Risk:Reward</b> → 1 : {_e(signal['rr_ratio'])}",
        f"💰 <b>Leverage</b>: {_e(signal['leverage'])}",
        "",
    ]

    # ── Confidence bar ────────────────────────────────────────────────────────
    bar = _confidence_bar(conf)
    lines += [
        f"📊 <b>CONFIDENCE</b>: {bar} {_e(conf)}%",
        "",
    ]

    # ── Setup reasons ─────────────────────────────────────────────────────────
    lines.append(f"🔍 <b>SETUP</b>")
    for r in reasons:
        lines.append(f"   ✅ {_e(r)}")

    # Indicator callouts
    extra = []
    if signal.get("rsi"):
        rsi_str = f"{signal['rsi']:.1f}"
        extra.append(f"RSI: {_e(rsi_str)}")
    if signal.get("adx"):
        adx_str = f"{signal['adx']:.0f}"
        extra.append(f"ADX: {_e(adx_str)}")
    if signal.get("vol_ratio"):
        vol_str = f"{signal['vol_ratio']:.1f}"
        extra.append(f"Vol ×{_e(vol_str)}")
    if signal.get("above_200") is True:
        extra.append("Above EMA200 ✅")
    elif signal.get("above_200") is False:
        extra.append("Below EMA200 ⚠️")
    if extra:
        lines.append(f"   📌 " + "  ·  ".join(extra))

    # ── Validity window ───────────────────────────────────────────────────────
    validity = "15–60 min" if style == "scalp" else "4–48 hours"
    lines += [
        "",
        f"🕐 <b>Valid for</b>: {validity}",
    ]

    # ── Hashtags ──────────────────────────────────────────────────────────────
    base = symbol.split("/")[0]
    tags = f"#{_e(base)} #{_e(d)} #{_e(style_label)} #{_e(exchange.upper())}"
    if market_type == "futures":
        tags += " #FUTURES"
    else:
        tags += " #SPOT"
    lines += ["", tags, "─" * 32]

    return "\n".join(lines)


def format_summary_report(signals_sent: int, scanned: int, exchange: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"📋 <b>Scan Report</b> — {_e(now)}\n"
        f"Exchange: {_e(exchange.upper())}\n"
        f"Pairs scanned: {_e(scanned)}\n"
        f"Signals sent: {_e(signals_sent)}\n"
    )


def format_startup_message(version: str = "1.0.0") -> str:
    return (
        f"🤖 <b>Crypto Signal Bot v{_e(version)} started</b>\n\n"
        f"✅ Multi-exchange scanner active\n"
        f"✅ Top-100 coins by market cap\n"
        f"✅ Scalping (1m/5m/15m) + Swing (1h/4h/1d)\n"
        f"✅ Spot + Futures markets\n\n"
        f"Signals will appear here automatically.\n"
        f"<i>Do your own research. This is not financial advice.</i>"
    )


def format_error_alert(context: str, error: str) -> str:
    return (
        f"⚠️ <b>Bot Error</b>\n"
        f"Context: {_e(context)}\n"
        f"Error: <code>{_e(error[:200])}</code>"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt(value: float) -> str:
    """Format a price with appropriate precision."""
    if value >= 1000:
        return f"{value:,.2f}"
    elif value >= 1:
        return f"{value:.4f}"
    elif value >= 0.01:
        return f"{value:.5f}"
    else:
        return f"{value:.8f}"


def _confidence_bar(conf: int, width: int = 10) -> str:
    filled = round(conf / 100 * width)
    return "█" * filled + "░" * (width - filled)
