"""
adaptive.py — Win-rate feedback loop (v3).

Adjusts confidence per (direction × style × strategy) based on realized
R over the lookback window, and pauses scanning after a streak of full
stop-outs.
"""
from datetime import datetime, timezone, timedelta

from src.database.db_logger import SessionLocal, SignalRecord, WIN_OUTCOMES
from config.settings import (
    ADAPTIVE_CONFIDENCE, ADAPTIVE_LOOKBACK_DAYS, ADAPTIVE_MIN_SIGNALS,
    LOSS_STREAK_PAUSE, LOSS_STREAK_COOLDOWN_MIN,
)
from config.logger import get_logger

logger = get_logger(__name__)


def get_confidence_multiplier(direction: str, style: str, strategy: str = None) -> float:
    """
    Multiplier from realized expectancy of this (direction, style, strategy)
    combo over the lookback window. Uses R, not raw win rate, so a 45%-WR
    strategy with big winners isn't punished.
    """
    if not ADAPTIVE_CONFIDENCE:
        return 1.0

    cutoff = datetime.now(timezone.utc) - timedelta(days=ADAPTIVE_LOOKBACK_DAYS)
    try:
        with SessionLocal() as db:
            q = db.query(SignalRecord).filter(
                SignalRecord.direction == direction,
                SignalRecord.style == style,
                SignalRecord.created_at >= cutoff,
                SignalRecord.outcome.isnot(None),
                SignalRecord.outcome.notin_(("NOFILL",)),
            )
            if strategy:
                q = q.filter(SignalRecord.strategy == strategy)
            closed = q.all()
            db.expunge_all()

        if len(closed) < ADAPTIVE_MIN_SIGNALS:
            return 1.0

        avg_r = sum(r.r_multiple or 0 for r in closed) / len(closed)
        if avg_r >= 0.5:
            mult = 1.05
        elif avg_r >= 0.0:
            mult = 1.0
        elif avg_r >= -0.3:
            mult = 0.9
        else:
            mult = 0.75

        logger.debug(f"Adaptive: {direction}/{style}/{strategy} "
                     f"avgR={avg_r:+.2f} ({len(closed)} trades) → ×{mult}")
        return mult
    except Exception as e:
        logger.debug(f"Adaptive error: {e}")
        return 1.0


def is_on_loss_cooldown() -> bool:
    """True if the last LOSS_STREAK_PAUSE filled trades were all full stop-outs
    and the most recent one closed within the cooldown window."""
    try:
        with SessionLocal() as db:
            recent = db.query(SignalRecord).filter(
                SignalRecord.outcome.isnot(None),
                SignalRecord.outcome.notin_(("NOFILL", "EXPIRED")),
                SignalRecord.sent_to_telegram.is_(True),
            ).order_by(SignalRecord.closed_at.desc()).limit(LOSS_STREAK_PAUSE).all()
            db.expunge_all()

        if len(recent) < LOSS_STREAK_PAUSE:
            return False
        if not all(r.outcome == "SL" for r in recent):
            return False

        last = recent[0].closed_at or recent[0].created_at
        if last and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last:
            elapsed = datetime.now(timezone.utc) - last
            if elapsed < timedelta(minutes=LOSS_STREAK_COOLDOWN_MIN):
                logger.info(f"Loss-streak cooldown: "
                            f"{LOSS_STREAK_COOLDOWN_MIN - int(elapsed.total_seconds()/60)}m remaining")
                return True
        return False
    except Exception as e:
        logger.debug(f"Loss cooldown check error: {e}")
        return False
