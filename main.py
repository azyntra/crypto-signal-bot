"""
main.py — Entry point for the Crypto Signal Bot v1.2.0
Starts APScheduler for:
  - Scalping scan    (every 5 min)
  - Swing scan       (every 60 min)
  - Outcome tracker  (every 60 sec)
  - Coin refresh     (every 30 min)
  - Daily report     (08:00 UTC daily)
Run with: python main.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval  import IntervalTrigger
from apscheduler.triggers.cron      import CronTrigger

from src.database.db_logger         import init_db
from src.scanner                    import run_scalp_scan, run_swing_scan
from src.tracking.outcome_tracker   import check_open_signals
from src.delivery.telegram_bot      import send_startup_message, build_application
from src.signals.formatter          import format_startup_message
from src.data.coin_universe         import fetch_top_coins

from config.settings import (
    SCALP_SCAN_INTERVAL_MIN,
    SWING_SCAN_INTERVAL_MIN,
    TOP_COINS_REFRESH_MIN,
)
from config.logger import get_logger

logger = get_logger(__name__)

VERSION = "1.2.0"


async def refresh_top_coins():
    logger.info("Refreshing top-100 coin list from CoinGecko...")
    fetch_top_coins()


async def send_daily_report():
    """Auto-post a daily performance digest to the channel at 08:00 UTC."""
    from src.tracking.performance import get_full_stats, format_performance_report
    from src.delivery.telegram_bot import send_signal
    stats = get_full_stats(days=1)
    if stats["total"] > 0:
        text = format_performance_report(stats)
        await send_signal(text)
        logger.info("Daily performance report sent.")
    else:
        logger.info("Daily report: no closed signals today, skipping.")


async def main():
    logger.info("╔══════════════════════════════════════╗")
    logger.info(f"║   Crypto Signal Bot v{VERSION}        ║")
    logger.info("╚══════════════════════════════════════╝")

    # Initialise database
    init_db()

    # Pre-load coin universe
    logger.info("Pre-loading top-100 coin list from CoinGecko...")
    fetch_top_coins()

    # ── Scheduler setup ───────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        run_scalp_scan,
        trigger=IntervalTrigger(minutes=SCALP_SCAN_INTERVAL_MIN),
        id="scalp_scan", name="Scalping Scanner",
        max_instances=1, misfire_grace_time=60,
    )

    scheduler.add_job(
        run_swing_scan,
        trigger=IntervalTrigger(minutes=SWING_SCAN_INTERVAL_MIN),
        id="swing_scan", name="Swing Scanner",
        max_instances=1, misfire_grace_time=120,
    )

    scheduler.add_job(
        check_open_signals,
        trigger=IntervalTrigger(seconds=60),
        id="outcome_tracker", name="Outcome Tracker",
        max_instances=1, misfire_grace_time=30,
    )

    scheduler.add_job(
        refresh_top_coins,
        trigger=IntervalTrigger(minutes=TOP_COINS_REFRESH_MIN),
        id="coin_refresh", name="Coin Universe Refresh",
    )

    scheduler.add_job(
        send_daily_report,
        trigger=CronTrigger(hour=8, minute=0, timezone="UTC"),
        id="daily_report", name="Daily Performance Report",
    )

    scheduler.start()
    logger.info(
        f"Scheduler started — "
        f"Scalp every {SCALP_SCAN_INTERVAL_MIN}m | "
        f"Swing every {SWING_SCAN_INTERVAL_MIN}m | "
        f"Outcome tracker every 60s | "
        f"Daily report at 08:00 UTC"
    )

    # Startup notification
    await send_startup_message(format_startup_message(VERSION))

    # Fire first scans immediately (don't wait for interval)
    logger.info("Running initial scans...")
    asyncio.create_task(run_scalp_scan())
    asyncio.create_task(run_swing_scan())

    # Start Telegram bot polling
    app = build_application()
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started. Press Ctrl+C to stop.")
        try:
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received.")
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()

    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
