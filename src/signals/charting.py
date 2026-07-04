"""
charting.py — Signal chart image generation (v3, new feature).

Renders a candlestick chart with entry zone, TP1-3, SL, and EMAs using
mplfinance. Returns PNG bytes for Telegram's send_photo.
"""
import io
from typing import Optional

import pandas as pd

from config.logger import get_logger

logger = get_logger(__name__)


def render_signal_chart(df: pd.DataFrame, signal: dict, symbol: str, timeframe: str) -> Optional[bytes]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import mplfinance as mpf

        data = df.iloc[-90:].copy()
        if len(data) < 30:
            return None

        direction = signal["direction"]
        up_color = "#26a69a"
        down_color = "#ef5350"

        mc = mpf.make_marketcolors(up=up_color, down=down_color, edge="inherit",
                                   wick="inherit", volume="in")
        style = mpf.make_mpf_style(base_mpf_style="nightclouds", marketcolors=mc,
                                   gridstyle=":", gridcolor="#333333")

        hlines = dict(
            hlines=[signal["tp3"], signal["tp2"], signal["tp1"],
                    signal["entry_high"], signal["entry_low"], signal["stop_loss"]],
            colors=["#00e676", "#66bb6a", "#a5d6a7",
                    "#ffd54f", "#ffd54f", "#ff5252"],
            linewidths=[1, 1, 1, 0.8, 0.8, 1.2],
            linestyle=["--", "--", "--", "-", "-", "-"],
        )

        addplots = []
        for span, color in ((21, "#42a5f5"), (50, "#ab47bc")):
            if len(data) > span:
                ema = data["close"].ewm(span=span).mean()
                addplots.append(mpf.make_addplot(ema, color=color, width=0.9))

        arrow = "▲ LONG" if direction == "LONG" else "▼ SHORT"
        title = f"{symbol} {timeframe} — {arrow} ({signal.get('strategy', '')})"

        buf = io.BytesIO()
        mpf.plot(
            data, type="candle", style=style, volume=True,
            hlines=hlines, addplot=addplots if addplots else None,
            title=title, figsize=(11, 6.5), tight_layout=True,
            savefig=dict(fname=buf, dpi=110, bbox_inches="tight",
                         facecolor="#0d1117"),
        )
        buf.seek(0)
        return buf.read()
    except ImportError:
        logger.warning("mplfinance/matplotlib not installed — charts disabled")
        return None
    except Exception as e:
        logger.error(f"Chart render error {symbol}: {e}")
        return None


def render_equity_curve(closed_records: list) -> Optional[bytes]:
    """Cumulative R equity curve from closed SignalRecords (chronological)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        pts = [(r.closed_at, r.r_multiple or 0) for r in closed_records if r.closed_at]
        if len(pts) < 2:
            return None
        pts.sort(key=lambda x: x[0])
        cum, series = 0.0, []
        for _, r in pts:
            cum += r
            series.append(cum)

        fig, ax = plt.subplots(figsize=(10, 5), facecolor="#0d1117")
        ax.set_facecolor("#0d1117")
        color = "#26a69a" if series[-1] >= 0 else "#ef5350"
        ax.plot(range(1, len(series) + 1), series, color=color, linewidth=1.8)
        ax.fill_between(range(1, len(series) + 1), series, alpha=0.15, color=color)
        ax.axhline(0, color="#666", linewidth=0.8, linestyle="--")
        ax.set_title(f"Equity Curve — {len(series)} trades, {series[-1]:+.1f}R total",
                     color="white")
        ax.set_xlabel("Trade #", color="#aaa")
        ax.set_ylabel("Cumulative R", color="#aaa")
        ax.tick_params(colors="#aaa")
        for spine in ax.spines.values():
            spine.set_color("#333")

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="#0d1117")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.error(f"Equity curve render error: {e}")
        return None
