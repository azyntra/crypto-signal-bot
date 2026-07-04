"""
main.py — Entry point for Crypto Signal Bot v3.

Schedules:
  - Intraday scan   : at :00/:15/:30/:45 +25s (right after 15m candle close)
  - Swing scan      : hourly at :01
  - Outcome tracker : every 60s (candle-accurate)
  - Coin refresh    : every 60 min
  - Daily report    : 08:00 UTC
  - AI market brief : 08:05 UTC
  - ML retrain      : Sunday 00:30 UTC

Run with: python main.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from src.database.db_logger import init_db
from src.scanner import run_intraday_scan, run_swing_scan
from src.tracking.outcome_tracker import check_open_signals
from src.delivery.telegram_bot import send_startup_message, send_signal, send_admin, build_application
from src.signals.formatter import format_startup_message
from src.data.coin_universe import fetch_top_coins
from config.settings import VERSION, AI_DAILY_BRIEF, TOP_COINS_REFRESH_MIN
from config.logger import get_logger

logger = get_logger(__name__)


async def refresh_top_coins():
    fetch_top_coins()


async def send_daily_report():
    from src.tracking.performance import get_full_stats, format_performance_report
    stats = get_full_stats(days=1)
    if stats["total"] > 0:
        await send_signal(format_performance_report(stats))
        logger.info("Daily performance report sent.")


async def send_daily_brief():
    """AI-generated market outlook for the channel."""
    if not AI_DAILY_BRIEF:
        return
    try:
        from src.analysis.regime import get_btc_regime
        from src.analysis.sentiment import get_fear_greed_index
        from src.analysis.ai_filter import generate_daily_brief
        from src.database.db_logger import get_stats

        btc = await get_btc_regime()
        fg = get_fear_greed_index()
        s = get_stats(days=7)
        context = {
            "btc_regime": btc.get("regime"),
            "btc_change_24h": None,
            "fear_greed": f"{fg.get('value')} ({fg.get('label')})" if fg else "N/A",
            "top_movers": "n/a",
            "stats": f"{s['total']} trades, {s['win_rate']}% WR, {s['total_r']:+.1f}R",
        }
        text = await generate_daily_brief(context)
        if text:
            await send_signal("🌅 <b>Daily Market Brief</b>\n\n" + text +
                              "\n\n<i>AI-generated — not financial advice.</i>")
    except Exception as e:
        logger.error(f"Daily brief failed: {e}")


async def weekly_ml_retrain():
    try:
        from src.analysis.ml_predictor import train_model
        stats = await asyncio.to_thread(train_model)
        if stats:
            await send_admin(
                f"🧠 <b>ML model retrained</b>\n"
                f"Signals: {stats['total_signals']} "
                f"({stats['wins']}W/{stats['losses']}L)\n"
                f"CV accuracy: {stats['cv_accuracy']}%")
    except Exception as e:
        logger.error(f"ML retrain failed: {e}")


async def main():
    logger.info(f"═══ Crypto Signal Bot v{VERSION} starting ═══")
    init_db()
    fetch_top_coins()

    scheduler = AsyncIOScheduler(timezone="UTC")

    # Scan right after candle close so indicators use fresh closed candles
    scheduler.add_job(run_intraday_scan,
                      trigger=CronTrigger(minute="0,15,30,45", second=25),
                      id="intraday_scan", max_instances=1, misfire_grace_time=120)
    scheduler.add_job(run_swing_scan,
                      trigger=CronTrigger(minute=1, second=10),
                      id="swing_scan", max_instances=1, misfire_grace_time=300)
    scheduler.add_job(check_open_signals,
                      trigger=IntervalTrigger(seconds=60),
                      id="outcome_tracker", max_instances=1, misfire_grace_time=30)
    scheduler.add_job(refresh_top_coins,
                      trigger=IntervalTrigger(minutes=TOP_COINS_REFRESH_MIN),
                      id="coin_refresh")
    scheduler.add_job(send_daily_report,
                      trigger=CronTrigger(hour=8, minute=0), id="daily_report")
    scheduler.add_job(send_daily_brief,
                      trigger=CronTrigger(hour=8, minute=5), id="daily_brief")
    scheduler.add_job(weekly_ml_retrain,
                      trigger=CronTrigger(day_of_week="sun", hour=0, minute=30),
                      id="ml_retrain")

    scheduler.start()
    logger.info("Scheduler started — intraday :00/:15/:30/:45, swing hourly, tracker 60s")

    await send_startup_message(format_startup_message(VERSION))

    app = build_application()
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram polling started. Ctrl+C to stop.")
        try:
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received.")
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()
            from src.data.fetcher import close_all
            await close_all()

    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
