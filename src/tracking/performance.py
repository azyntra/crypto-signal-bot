"""
performance.py — Honest performance statistics (v3).

Reports expectancy in R, profit factor, and max drawdown — not just win
rate. A 60% win rate with -0.2R expectancy loses money; these stats make
that visible instead of hiding it.
"""
from datetime import datetime, timezone, timedelta

from src.database.db_logger import SessionLocal, SignalRecord, WIN_OUTCOMES
from config.logger import get_logger

logger = get_logger(__name__)

CLOSED_OUTCOMES = ("TP1", "TP2", "TP3", "SL", "EXPIRED")


def get_full_stats(days: int = 7) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with SessionLocal() as db:
        rows = db.query(SignalRecord).filter(
            SignalRecord.created_at >= cutoff,
        ).order_by(SignalRecord.closed_at.asc()).all()
        db.expunge_all()

    closed = [r for r in rows if r.outcome in CLOSED_OUTCOMES]
    open_ = [r for r in rows if r.outcome is None and r.sent_to_telegram]
    nofill = [r for r in rows if r.outcome == "NOFILL"]

    wins = [r for r in closed if (r.r_multiple or 0) > 0.05]
    losses = [r for r in closed if (r.r_multiple or 0) < -0.05]
    flats = [r for r in closed if r not in wins and r not in losses]

    rs = [r.r_multiple or 0 for r in closed]
    total_r = sum(rs)
    gross_win = sum(x for x in rs if x > 0)
    gross_loss = abs(sum(x for x in rs if x < 0))

    # max drawdown on cumulative R
    cum = peak = max_dd = 0.0
    for x in rs:
        cum += x
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    by_strategy, by_symbol = {}, {}
    for r in closed:
        for key, bucket in ((r.strategy or "unknown", by_strategy), (r.symbol, by_symbol)):
            b = bucket.setdefault(key, {"total": 0, "wins": 0, "losses": 0, "r": 0.0})
            b["total"] += 1
            b["r"] += r.r_multiple or 0
            if (r.r_multiple or 0) > 0.05:
                b["wins"] += 1
            elif (r.r_multiple or 0) < -0.05:
                b["losses"] += 1
    for bucket in (by_strategy, by_symbol):
        for b in bucket.values():
            b["win_rate"] = round(b["wins"] / b["total"] * 100, 1) if b["total"] else 0
            b["avg_r"] = round(b["r"] / b["total"], 2) if b["total"] else 0
            b["avg_pnl"] = b["avg_r"]  # back-compat for /best command

    return {
        "days": days,
        "total": len(closed), "open": len(open_), "nofill": len(nofill),
        "wins": len(wins), "losses": len(losses), "flats": len(flats),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_r": round(total_r, 2),
        "expectancy_r": round(total_r / len(closed), 3) if closed else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else (round(gross_win, 2) if gross_win else 0),
        "max_drawdown_r": round(max_dd, 2),
        "avg_mfe": round(sum(r.mfe_pct or 0 for r in closed) / len(closed), 2) if closed else 0,
        "avg_mae": round(sum(r.mae_pct or 0 for r in closed) / len(closed), 2) if closed else 0,
        "by_strategy": by_strategy,
        "by_symbol": by_symbol,
        "closed_records": closed,
    }


def format_performance_report(s: dict) -> str:
    wr = s["win_rate"]
    bar = "█" * round(wr / 100 * 8) + "░" * (8 - round(wr / 100 * 8))
    exp = s["expectancy_r"]
    verdict = "🟢 Profitable edge" if exp > 0.1 else ("🟡 Marginal" if exp > -0.05 else "🔴 Losing — review needed")

    lines = [
        "─" * 30,
        f"📊 <b>Performance — last {s['days']}d</b>",
        "─" * 30,
        "",
        f"Closed: {s['total']}  (✅{s['wins']} / ❌{s['losses']} / ⚪{s['flats']})",
        f"Open: {s['open']}   Unfilled: {s['nofill']}",
        "",
        f"Win rate:    {bar} <b>{wr}%</b>",
        f"Net result:  <b>{s['total_r']:+.2f}R</b>",
        f"Expectancy:  <b>{exp:+.3f}R</b> per trade",
        f"Profit factor: {s['profit_factor']}",
        f"Max drawdown: {s['max_drawdown_r']}R",
        "",
        verdict,
    ]

    if s["by_strategy"]:
        lines += ["", "<b>By strategy:</b>"]
        for name, b in sorted(s["by_strategy"].items(), key=lambda x: -x[1]["r"]):
            lines.append(f" • {name}: {b['wins']}W/{b['losses']}L "
                         f"({b['win_rate']}%)  {b['r']:+.1f}R")

    lines.append("─" * 30)
    return "\n".join(lines)
