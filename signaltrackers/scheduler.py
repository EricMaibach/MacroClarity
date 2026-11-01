from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from datetime import datetime, timedelta
import logging
import os

logger = logging.getLogger(__name__)

# Scheduler instance (singleton)
scheduler = None

def init_scheduler(app):
    """Initialize APScheduler with Flask app context"""
    global scheduler

    if scheduler is not None:
        return scheduler

    # Executor configuration
    executors = {
        'default': ThreadPoolExecutor(max_workers=3)
    }

    # Job defaults
    job_defaults = {
        'coalesce': True,  # Combine multiple missed executions into one
        'max_instances': 1,  # Prevent concurrent execution of same job
        'misfire_grace_time': 300  # 5 minutes grace period
    }

    # Use in-memory job store (jobs won't persist across restarts, but that's okay
    # since we register them on startup anyway)
    scheduler = BackgroundScheduler(
        executors=executors,
        job_defaults=job_defaults,
        timezone='UTC'
    )

    # Register jobs
    register_jobs(app)

    # Start scheduler
    scheduler.start()
    logger.info("APScheduler started successfully")

    return scheduler


def register_jobs(app):
    """Register all scheduled jobs"""
    # Import job wrapper that will handle app context
    from jobs.alert_jobs import check_alert_thresholds_wrapper
    from jobs.email_jobs import send_weekly_reviews_wrapper
    from jobs.sector_tone_jobs import run_sector_tone_pipeline_wrapper

    # Alert checking - every 15 minutes
    scheduler.add_job(
        func=check_alert_thresholds_wrapper,
        trigger='interval',
        minutes=15,
        id='check_alerts',
        name='Check alert thresholds',
        replace_existing=True
    )
    logger.info("Registered job: check_alerts (every 15 minutes)")

    # Weekly review email (US-490.5) - runs every 15 minutes and emails users
    # who are due: Saturday 08:00+ in their timezone, once this week's review exists.
    scheduler.add_job(
        func=send_weekly_reviews_wrapper,
        trigger='interval',
        minutes=15,
        # Id kept from the old daily email: the job store is persistent, and
        # replace_existing swaps the function in place under the same id.
        id='daily_briefings',
        name='Send weekly review emails',
        replace_existing=True
    )
    logger.info("Registered job: daily_briefings -> weekly review email (every 15 minutes)")

    # Quarterly sector management tone pipeline
    # Runs at 02:00 UTC on the 1st of Jan, Apr, Jul, Oct (start of each calendar quarter)
    scheduler.add_job(
        func=run_sector_tone_pipeline_wrapper,
        trigger='cron',
        month='1,4,7,10',
        day=1,
        hour=2,
        minute=0,
        id='sector_tone_quarterly',
        name='Quarterly sector management tone update',
        replace_existing=True
    )
    logger.info("Registered job: sector_tone_quarterly (Jan/Apr/Jul/Oct 1 at 02:00 UTC)")

    # Startup seed check — queue a one-time pipeline run if the cache is absent or empty
    _check_and_seed_sector_tone(run_sector_tone_pipeline_wrapper)


def _check_and_seed_sector_tone(wrapper_func):
    """Submit a one-time seed job if the sector tone cache is absent or empty.

    The seed job runs ~5 seconds after startup (date trigger) so Flask can
    finish initialising before the heavy FinBERT pipeline starts.
    """
    cache_empty = _is_sector_tone_cache_empty()

    if cache_empty:
        logger.info("Sector tone cache empty — queuing immediate seed run")
        scheduler.add_job(
            func=wrapper_func,
            trigger='date',
            run_date=datetime.utcnow() + timedelta(seconds=5),
            id='sector_tone_seed',
            name='Sector tone pipeline seed run (one-time)',
            replace_existing=True
        )
    else:
        logger.info("Sector tone cache populated — skipping seed run")


def _is_sector_tone_cache_empty() -> bool:
    """Return True if the sector tone cache is absent, empty, or has no data."""
    try:
        from sector_tone_pipeline import get_sector_management_tone
        cache = get_sector_management_tone()
        if cache is None:
            return True
        if not cache.get('data_available'):
            return True
        if not cache.get('sectors'):
            return True
        return False
    except Exception as exc:
        logger.warning("Could not read sector tone cache during startup check: %s", exc)
        return True


def shutdown_scheduler():
    """Gracefully shutdown scheduler"""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=True)
        logger.info("APScheduler shut down successfully")
