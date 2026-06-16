"""
adaptive.py — Win-rate feedback loop for confidence adjustment.
Queries recent signal performance and adjusts confidence multiplier
based on how well a (direction × style × exchange) combo has been performing.

Also supports blocking a direction entirely if win rate is dangerously low.
"""
from datetime import datetime, timezone, timedelta

from src.database.db_logger import SessionLocal, SignalRecord
from config.settings import (
    ADAPTIVE_CONFIDENCE, ADAPTIVE_LOOKBACK_DAYS,
    ADAPTIVE_MIN_SIGNALS, ADAPTIVE_BLOCK_WINRATE,
    LOSS_STREAK_PAUSE, LOSS_STREAK_COOLDOWN_MIN,
)
from config.logger import get_logger

logger = get_logger(__name__)


def get_confidence_multiplier(direction: str, style: str, exchange: str) -> float:
    """
    Query recent win rate for (direction, style, exchange) and return a multiplier.

    | Win Rate   | Multiplier |
    |------------|------------|
    | > 60%      | 1.1        |
    | 40–60%     | 1.0        |
    | 20–40%     | 0.8        |
    | < 20%      | 0.6        |
    """
    if not ADAPTIVE_CONFIDENCE:
        return 1.0

    cutoff = datetime.now(timezone.utc) - timedelta(days=ADAPTIVE_LOOKBACK_DAYS)

    try:
        with SessionLocal() as db:
            closed = db.query(SignalRecord).filter(
                SignalRecord.direction == direction,
                SignalRecord.style == style,
                SignalRecord.exchange == exchange,
                SignalRecord.created_at >= cutoff,
                SignalRecord.outcome.isnot(None),
                SignalRecord.outcome != "EXPIRED",
            ).all()
            db.expunge_all()

        total = len(closed)
        if total < ADAPTIVE_MIN_SIGNALS:
            return 1.0  # not enough data yet

        wins = sum(1 for r in closed if r.outcome in ("TP1", "TP2", "TP3"))
        win_rate = wins / total * 100

        if win_rate > 60:
            mult = 1.1
        elif win_rate >= 40:
            mult = 1.0
        elif win_rate >= 20:
            mult = 0.8
        else:
            mult = 0.6

        logger.debug(f"Adaptive: {direction}/{style}/{exchange} "
                     f"WR={win_rate:.0f}% ({wins}W/{total}T) → mult={mult}")
        return mult

    except Exception as e:
        logger.debug(f"Adaptive confidence error: {e}")
        return 1.0


def is_direction_blocked(direction: str, style: str) -> bool:
    """
    Block a direction entirely if win rate < ADAPTIVE_BLOCK_WINRATE%
    across ALL exchanges (with at least ADAPTIVE_MIN_SIGNALS signals).
    """
    if not ADAPTIVE_CONFIDENCE:
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(days=ADAPTIVE_LOOKBACK_DAYS)

    try:
        with SessionLocal() as db:
            closed = db.query(SignalRecord).filter(
                SignalRecord.direction == direction,
                SignalRecord.style == style,
                SignalRecord.created_at >= cutoff,
                SignalRecord.outcome.isnot(None),
                SignalRecord.outcome != "EXPIRED",
            ).all()
            db.expunge_all()

        total = len(closed)
        if total < ADAPTIVE_MIN_SIGNALS:
            return False

        wins = sum(1 for r in closed if r.outcome in ("TP1", "TP2", "TP3"))
        win_rate = wins / total * 100

        if win_rate < ADAPTIVE_BLOCK_WINRATE:
            logger.info(f"Adaptive: {direction}/{style} BLOCKED — "
                        f"WR={win_rate:.0f}% ({wins}W/{total}T)")
            return True

        return False

    except Exception as e:
        logger.debug(f"Adaptive block check error: {e}")
        return False


def is_on_loss_cooldown() -> bool:
    """
    Check if the last N signals were all losses (SL).
    If so, the bot should pause scanning.
    Returns True if in cooldown period.
    """
    try:
        with SessionLocal() as db:
            recent = db.query(SignalRecord).filter(
                SignalRecord.outcome.isnot(None),
                SignalRecord.outcome != "EXPIRED",
                SignalRecord.sent_to_telegram.is_(True),
            ).order_by(
                SignalRecord.closed_at.desc()
            ).limit(LOSS_STREAK_PAUSE).all()
            db.expunge_all()

        if len(recent) < LOSS_STREAK_PAUSE:
            return False

        # Check if ALL recent signals are losses
        all_losses = all(r.outcome == "SL" for r in recent)
        if not all_losses:
            return False

        # Check if the last loss was recent enough for cooldown
        last_loss_time = recent[0].closed_at or recent[0].created_at
        if last_loss_time and last_loss_time.tzinfo is None:
            last_loss_time = last_loss_time.replace(tzinfo=timezone.utc)

        if last_loss_time:
            elapsed = datetime.now(timezone.utc) - last_loss_time
            if elapsed < timedelta(minutes=LOSS_STREAK_COOLDOWN_MIN):
                remaining = LOSS_STREAK_COOLDOWN_MIN - int(elapsed.total_seconds() / 60)
                logger.info(f"Loss streak cooldown: {remaining}m remaining "
                            f"({LOSS_STREAK_PAUSE} consecutive SLs)")
                return True

        return False

    except Exception as e:
        logger.debug(f"Loss cooldown check error: {e}")
        return False
