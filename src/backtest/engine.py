"""
engine.py — Backtest engine (v3, new feature).

Runs the EXACT same strategy + validator + exit model that the live bot
uses, bar-by-bar over historical data, so the reported win rate and
expectancy actually predict live behaviour.

Notes / limitations (honest ones):
  - Fill and TP/SL detection use entry-timeframe candles, not 1m candles,
    so results are slightly optimistic on wick-heavy pairs.
  - No AI/ML/sentiment layers (not reproducible historically). The
    backtest measures the rule engine — the live layers only remove
    signals, so live quality should be >= backtest quality.
  - Uses the pessimistic same-candle rule: if a candle touches SL and TP,
    SL counts first.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from src.data.fetcher import fetch_ohlcv_history
from src.analysis.indicators import compute_indicators
from src.analysis.strategies import evaluate
from src.analysis.regime import classify_regime
from src.signals.validator import validate_and_build
from config.settings import (
    TRACK_EXCHANGE, MARKET_TYPE, BACKTEST_MAX_DAYS, TP_PORTIONS,
    FILL_EXPIRY_HOURS, TRADE_EXPIRY_HOURS,
)
from config.logger import get_logger

logger = get_logger(__name__)

_TF_MIN = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}


@dataclass
class BTrade:
    entry_time: pd.Timestamp
    direction: str
    strategy: str
    fill: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    outcome: str = ""
    r_multiple: float = 0.0
    bars_held: int = 0


@dataclass
class BTResult:
    symbol: str
    style: str
    days: int
    trades: list = field(default_factory=list)
    signals_generated: int = 0
    signals_unfilled: int = 0

    def stats(self) -> dict:
        t = self.trades
        rs = [x.r_multiple for x in t]
        wins = [x for x in t if x.r_multiple > 0.05]
        losses = [x for x in t if x.r_multiple < -0.05]
        gross_w = sum(x for x in rs if x > 0)
        gross_l = abs(sum(x for x in rs if x < 0))
        cum = peak = dd = 0.0
        for x in rs:
            cum += x
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        return {
            "symbol": self.symbol, "style": self.style, "days": self.days,
            "signals": self.signals_generated, "unfilled": self.signals_unfilled,
            "trades": len(t), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(t) * 100, 1) if t else 0,
            "total_r": round(sum(rs), 2),
            "expectancy_r": round(sum(rs) / len(t), 3) if t else 0,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else round(gross_w, 2),
            "max_drawdown_r": round(dd, 2),
        }


def _simulate_trade(candles: pd.DataFrame, start_i: int, sig: dict,
                    style: str, tf_min: int) -> tuple[Optional[BTrade], int]:
    """Simulate fill + exits from bar start_i+1 forward. Returns (trade, bars_used)."""
    direction = sig["direction"]
    sign = 1 if direction == "LONG" else -1
    lo, hi = sig["entry_low"], sig["entry_high"]
    fill_deadline = start_i + max(int(FILL_EXPIRY_HOURS.get(style, 4) * 60 / tf_min), 1)
    trade_deadline_bars = max(int(TRADE_EXPIRY_HOURS.get(style, 48) * 60 / tf_min), 4)

    fill_i, fill_px = None, None
    n = len(candles)

    for i in range(start_i + 1, min(fill_deadline + 1, n)):
        c = candles.iloc[i]
        if c["low"] <= hi and c["high"] >= lo:
            fill_i = i
            fill_px = min(max(c["open"], lo), hi)
            break
        # ran to TP1 without fill
        if (sign == 1 and c["high"] >= sig["tp1"]) or (sign == -1 and c["low"] <= sig["tp1"]):
            return None, i - start_i

    if fill_i is None:
        return None, min(fill_deadline, n - 1) - start_i

    trade = BTrade(candles.index[fill_i], direction, sig.get("strategy", ""),
                   fill_px, sig["stop_loss"], sig["tp1"], sig["tp2"], sig["tp3"])
    risk = abs(fill_px - sig["stop_loss"])
    if risk == 0:
        return None, 1

    highest_tp = None
    adj_sl = None
    p1, p2, p3 = TP_PORTIONS

    def realized(final_exit):
        exits = []
        if highest_tp in ("TP1", "TP2", "TP3"): exits.append((p1, sig["tp1"]))
        if highest_tp in ("TP2", "TP3"):        exits.append((p2, sig["tp2"]))
        if highest_tp == "TP3":                 exits.append((p3, sig["tp3"]))
        used = sum(p for p, _ in exits)
        if used < 0.999:
            exits.append((1 - used, final_exit))
        return sum(p * (sign * (px - fill_px) / risk) for p, px in exits)

    end_i = min(fill_i + trade_deadline_bars, n - 1)
    for i in range(fill_i, end_i + 1):
        c = candles.iloc[i]
        eff_sl = adj_sl if adj_sl is not None else sig["stop_loss"]
        sl_hit = (c["low"] <= eff_sl) if sign == 1 else (c["high"] >= eff_sl)
        if sl_hit:
            trade.outcome = highest_tp or "SL"
            trade.r_multiple = round(realized(eff_sl), 3)
            trade.bars_held = i - fill_i
            return trade, i - start_i

        def hit(level):
            return (c["high"] >= level) if sign == 1 else (c["low"] <= level)

        if hit(sig["tp3"]):
            highest_tp = "TP3"
            trade.outcome = "TP3"
            trade.r_multiple = round(realized(sig["tp3"]), 3)
            trade.bars_held = i - fill_i
            return trade, i - start_i
        if hit(sig["tp2"]) and highest_tp != "TP2":
            highest_tp, adj_sl = "TP2", sig["tp1"]
        elif hit(sig["tp1"]) and highest_tp is None:
            highest_tp, adj_sl = "TP1", fill_px

    # expired
    last_close = float(candles.iloc[end_i]["close"])
    trade.outcome = "EXPIRED"
    trade.r_multiple = round(realized(last_close), 3)
    trade.bars_held = end_i - fill_i
    return trade, end_i - start_i


async def run_backtest(symbol: str, style: str = "intraday",
                       days: int = 30, exchange: str = TRACK_EXCHANGE) -> Optional[BTResult]:
    days = min(days, BACKTEST_MAX_DAYS)
    entry_tf = "15m" if style == "intraday" else "1h"
    htf_tf = "1h" if style == "intraday" else "4h"
    regime_tf = "4h"
    tf_min = _TF_MIN[entry_tf]

    # extra history so indicators have warmup
    warmup_days = days + max(10, int(300 * tf_min / 1440) + 5)
    dfs = {}
    for tf in {entry_tf, htf_tf, regime_tf}:
        dfs[tf] = await fetch_ohlcv_history(exchange, symbol, tf, warmup_days, MARKET_TYPE)
        if dfs[tf] is None:
            logger.warning(f"Backtest: no {tf} data for {symbol}")
            return None

    entry_df = dfs[entry_tf]
    start_ts = entry_df.index[-1] - pd.Timedelta(days=days)
    result = BTResult(symbol=symbol, style=style, days=days)

    i = 300  # warmup bars
    n = len(entry_df)
    while i < n - 2:
        ts = entry_df.index[i]
        if ts < start_ts:
            i += 1
            continue

        window = entry_df.iloc[: i + 1]
        # slice higher TFs up to current time (compute_indicators drops last/forming bar)
        htf_window = dfs[htf_tf][dfs[htf_tf].index <= ts]
        regime_window = dfs[regime_tf][dfs[regime_tf].index <= ts]

        ind_entry = compute_indicators(window)
        ind_htf = compute_indicators(htf_window)
        ind_regime = compute_indicators(regime_window)
        if not ind_entry or not ind_regime:
            i += 1
            continue

        regime = classify_regime(ind_regime, ind_htf)
        if regime == "choppy":
            i += 1
            continue

        cand = evaluate(ind_entry, ind_htf, regime)
        if not cand:
            i += 1
            continue

        sig = validate_and_build(cand, style)
        if not sig:
            i += 1
            continue

        result.signals_generated += 1
        trade, bars_used = _simulate_trade(entry_df, i, sig, style, tf_min)
        if trade is None:
            result.signals_unfilled += 1
            i += max(bars_used, 1)
        else:
            result.trades.append(trade)
            i += max(bars_used, 1)   # no overlapping trades on the same symbol

    return result


def format_backtest_report(stats: dict) -> str:
    exp = stats["expectancy_r"]
    verdict = ("🟢 Positive edge" if exp > 0.1 else
               "🟡 Marginal edge" if exp > 0 else "🔴 No edge on this pair")
    return "\n".join([
        "─" * 30,
        f"🧪 <b>Backtest — {stats['symbol']} ({stats['style']}, {stats['days']}d)</b>",
        "─" * 30,
        "",
        f"Signals generated: {stats['signals']}  (unfilled: {stats['unfilled']})",
        f"Trades: {stats['trades']}  (✅{stats['wins']} / ❌{stats['losses']})",
        f"Win rate: <b>{stats['win_rate']}%</b>",
        f"Net: <b>{stats['total_r']:+.2f}R</b>  ·  Expectancy: <b>{exp:+.3f}R</b>",
        f"Profit factor: {stats['profit_factor']}  ·  Max DD: {stats['max_drawdown_r']}R",
        "",
        verdict,
        "",
        "<i>Rule engine only (no AI/ML layers). Entry-TF candle resolution.</i>",
        "─" * 30,
    ])
