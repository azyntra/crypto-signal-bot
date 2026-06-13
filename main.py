"""
main.py — Entry point for the Crypto Signal Bot.
Starts APScheduler for timed scans + Telegram bot for commands.
Run with: python main.py
"""
import asyncio
import sys
import os

# Make sure repo root is on PYTHONPATH
sys.path.insert(0, os.path.dirname(__file__))

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval  import IntervalTrigger

from src.database.db_logger     import init_db
from src.scanner                 import run_scalp_scan, run_swing_scan
from src.delivery.telegram_bot   import send_startup_message, build_application
from src.signals.formatter       import format_startup_message
from src.data.coin_universe      import fetch_top_coins

from config.settings import (
    SCALP_SCAN_INTERVAL_MIN, SWING_SCAN_INTERVAL_MIN,
    TOP_COINS_REFRESH_MIN,
)
from config.logger import get_logger

logger = get_logger(__name__)

VERSION = "1.0.0"


async def refresh_top_coins():
    """Wrapper for scheduled top-coin list refresh."""
    logger.info("Refreshing top-100 coin list...")
    fetch_top_coins()


async def main():
    logger.info(f"╔══════════════════════════════════════╗")
    logger.info(f"║   Crypto Signal Bot v{VERSION}          ║")
    logger.info(f"╚══════════════════════════════════════╝")

    # Init DB
    init_db()

    # Pre-load coin list
    logger.info("Pre-loading top-100 coin list from CoinGecko...")
    fetch_top_coins()

    # Scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        run_scalp_scan,
        trigger  = IntervalTrigger(minutes=SCALP_SCAN_INTERVAL_MIN),
        id       = "scalp_scan",
        name     = "Scalping Scanner",
        max_instances = 1,
        misfire_grace_time = 60,
    )

    scheduler.add_job(
        run_swing_scan,
        trigger  = IntervalTrigger(minutes=SWING_SCAN_INTERVAL_MIN),
        id       = "swing_scan",
        name     = "Swing Scanner",
        max_instances = 1,
        misfire_grace_time = 120,
    )

    scheduler.add_job(
        refresh_top_coins,
        trigger  = IntervalTrigger(minutes=TOP_COINS_REFRESH_MIN),
        id       = "coin_refresh",
        name     = "Coin Universe Refresh",
    )

    scheduler.start()
    logger.info(
        f"Scheduler started — "
        f"Scalp every {SCALP_SCAN_INTERVAL_MIN}m, "
        f"Swing every {SWING_SCAN_INTERVAL_MIN}m"
    )

    # Send startup notification
    await send_startup_message(format_startup_message(VERSION))

    # Run first scans immediately
    logger.info("Running initial scans...")
    asyncio.create_task(run_scalp_scan())
    asyncio.create_task(run_swing_scan())

    # Start Telegram bot (handles /start /stats /status commands)
    app = build_application()
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        logger.info("Bot polling started. Press Ctrl+C to stop.")
        try:
            # Keep alive
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
