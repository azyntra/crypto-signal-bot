"""
performance.py — Detailed performance analytics from the signals DB.
Breakdowns by: style, exchange, direction, symbol, TP level.
Used by /report and /best Telegram commands + daily digest.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict

from src.database.db_logger import SessionLocal, SignalRecord
from config.logger import get_logger

logger = get_logger(__name__)


def get_full_stats(days: int = 7) -> dict:
    """Return comprehensive performance stats for the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    with SessionLocal() as db:
        closed = db.query(SignalRecord).filter(
            SignalRecord.created_at >= cutoff,
            SignalRecord.outcome.isnot(None),
            SignalRecord.outcome != "EXPIRED",
        ).all()

        open_count = db.query(SignalRecord).filter(
            SignalRecord.created_at >= cutoff,
            SignalRecord.outcome.is_(None),
        ).count()

        expired_count = db.query(SignalRecord).filter(
            SignalRecord.created_at >= cutoff,
            SignalRecord.outcome == "EXPIRED",
        ).count()

        db.expunge_all()

    if not closed:
        return _empty_stats(days, open_count, expired_count)

    wins   = [r for r in closed if r.outcome in ("TP1", "TP2", "TP3")]
    losses = [r for r in closed if r.outcome == "SL"]
    total  = len(closed)

    win_rate   = len(wins) / total * 100 if total else 0
    avg_profit = sum(r.profit_pct for r in wins   if r.profit_pct) / len(wins)   if wins   else 0
    avg_loss   = sum(r.profit_pct for r in losses if r.profit_pct) / len(losses) if losses else 0
    total_pnl  = sum(r.profit_pct for r in closed if r.profit_pct)

    sorted_pnl = sorted(closed, key=lambda r: r.profit_pct or 0)

    by_tp = defaultdict(int)
    for r in closed:
        by_tp[r.outcome] += 1

    return {
        "days":         days,
        "total":        total,
        "open":         open_count,
        "expired":      expired_count,
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     round(win_rate, 1),
        "avg_profit":   round(avg_profit, 2),
        "avg_loss":     round(avg_loss, 2),
        "total_pnl":    round(total_pnl, 2),
        "best_trade":   _trade_summary(sorted_pnl[-1] if sorted_pnl else None),
        "worst_trade":  _trade_summary(sorted_pnl[0]  if sorted_pnl else None),
        "by_style":     _breakdown(closed, lambda r: r.style),
        "by_exchange":  _breakdown(closed, lambda r: r.exchange),
        "by_direction": _breakdown(closed, lambda r: r.direction),
        "by_symbol":    dict(sorted(
            _breakdown(closed, lambda r: r.symbol).items(),
            key=lambda x: x[1]["total"], reverse=True
        )[:10]),
        "by_tp_level":  dict(by_tp),
    }


def _empty_stats(days, open_count, expired_count) -> dict:
    return {
        "days": days, "total": 0, "open": open_count, "expired": expired_count,
        "wins": 0, "losses": 0, "win_rate": 0,
        "avg_profit": 0, "avg_loss": 0, "total_pnl": 0,
        "best_trade": None, "worst_trade": None,
        "by_style": {}, "by_exchange": {}, "by_direction": {},
        "by_symbol": {}, "by_tp_level": {},
    }


def _breakdown(records: list, key_fn) -> dict:
    groups = defaultdict(list)
    for r in records:
        groups[key_fn(r)].append(r)
    result = {}
    for k, recs in groups.items():
        w = [r for r in recs if r.outcome in ("TP1", "TP2", "TP3")]
        l = [r for r in recs if r.outcome == "SL"]
        t = len(recs)
        result[k] = {
            "total":    t,
            "wins":     len(w),
            "losses":   len(l),
            "win_rate": round(len(w) / t * 100, 1) if t else 0,
            "avg_pnl":  round(sum(r.profit_pct or 0 for r in recs) / t, 2) if t else 0,
        }
    return result


def _trade_summary(r: Optional[SignalRecord]) -> Optional[dict]:
    if not r:
        return None
    return {
        "symbol":     r.symbol,
        "direction":  r.direction,
        "outcome":    r.outcome,
        "profit_pct": r.profit_pct,
        "style":      r.style,
        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else None,
    }


# ── Telegram-formatted report ──────────────────────────────────────────────────

def _bar(pct: float, width: int = 8) -> str:
    filled = round(min(max(pct, 0), 100) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def format_performance_report(stats: dict) -> str:
    wr       = stats["win_rate"]
    pnl      = stats["total_pnl"]
    pnl_sign = "+" if pnl >= 0 else ""
    pnl_icon = "📈" if pnl >= 0 else "📉"

    lines = [
        "─" * 32,
        f"📊 <b>PERFORMANCE REPORT — Last {stats['days']}d</b>",
        "─" * 32,
        "",
        "🎯 <b>Overview</b>",
        f"   Closed signals:  <b>{stats['total']}</b>",
        f"   ✅ Wins (TP):     <b>{stats['wins']}</b>",
        f"   ❌ Losses (SL):   <b>{stats['losses']}</b>",
        f"   ⏰ Expired:       {stats['expired']}",
        f"   🔓 Still open:    {stats['open']}",
        f"   Win rate:  {_bar(wr)} <b>{wr}%</b>",
        "",
        f"{pnl_icon} <b>P&amp;L Summary</b>",
        f"   Total P&amp;L:    <b>{pnl_sign}{pnl:.2f}%</b>",
        f"   Avg win:    +{stats['avg_profit']:.2f}%",
        f"   Avg loss:   {stats['avg_loss']:.2f}%",
        "",
    ]

    # TP level distribution
    tp = stats.get("by_tp_level", {})
    if tp:
        lines += [
            "🎯 <b>Exit Breakdown</b>",
            f"   TP1: {tp.get('TP1',0)}  TP2: {tp.get('TP2',0)}  TP3: {tp.get('TP3',0)}  SL: {tp.get('SL',0)}",
            "",
        ]

    # By style
    bs = stats.get("by_style", {})
    if bs:
        lines.append("⚡ <b>By Style</b>")
        for style, s in sorted(bs.items()):
            lines.append(
                f"   {style.upper():6s}: {s['wins']}W {s['losses']}L  "
                f"WR:{s['win_rate']}%  Avg:{s['avg_pnl']:+.2f}%"
            )
        lines.append("")

    # By direction
    bd = stats.get("by_direction", {})
    if bd:
        lines.append("↕️ <b>By Direction</b>")
        for d, s in sorted(bd.items()):
            lines.append(f"   {d:5s}: {s['wins']}W {s['losses']}L  WR:{s['win_rate']}%")
        lines.append("")

    # By exchange
    be = stats.get("by_exchange", {})
    if be:
        lines.append("🏦 <b>By Exchange</b>")
        for ex, s in sorted(be.items()):
            lines.append(f"   {ex.upper():8s}: {s['wins']}W {s['losses']}L  WR:{s['win_rate']}%")
        lines.append("")

    # Best / worst
    best  = stats.get("best_trade")
    worst = stats.get("worst_trade")
    if best:
        lines.append(
            f"🏆 <b>Best</b>:  {best['symbol']} {best['direction']} "
            f"{best['outcome']}  <b>+{best['profit_pct']:.2f}%</b>"
        )
    if worst and worst != best:
        lines.append(
            f"💀 <b>Worst</b>: {worst['symbol']} {worst['direction']} "
            f"{worst['outcome']}  <b>{worst['profit_pct']:.2f}%</b>"
        )

    # Top symbols
    sym = stats.get("by_symbol", {})
    if sym:
        lines += ["", "🪙 <b>Top Symbols</b>"]
        for symbol, s in list(sym.items())[:5]:
            lines.append(
                f"   {symbol}: {s['wins']}W {s['losses']}L  "
                f"WR:{s['win_rate']}%  Avg:{s['avg_pnl']:+.2f}%"
            )

    lines += ["", "─" * 32]
    return "\n".join(lines)
