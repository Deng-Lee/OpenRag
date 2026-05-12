"""Background scheduler for periodic tasks

This module provides scheduled tasks that run in the background:
1. Automatic timeout recovery for stuck tasks
2. Periodic cleanup and maintenance
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session
import logging

from openrag.database import SessionLocal, get_engine
from openrag.broker import TaskBroker

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: BackgroundScheduler = None


def create_scheduler() -> BackgroundScheduler:
    """Create and configure the background scheduler"""
    scheduler = BackgroundScheduler()

    # Add timeout recovery job - runs every 60 seconds
    scheduler.add_job(
        func=recover_timeout_tasks_job,
        trigger=IntervalTrigger(seconds=60),
        id='recover_timeout_tasks',
        name='Recover timeout tasks',
        replace_existing=True,
    )

    return scheduler


def recover_timeout_tasks_job():
    """Job to recover timeout tasks

    This job runs periodically to find tasks that haven't received
    a heartbeat for more than 10 minutes and resets them to pending.
    """
    # Configure session with engine before use
    SessionLocal.configure(bind=get_engine())
    db = SessionLocal()
    try:
        broker = TaskBroker(db)
        count = broker.recover_timeout_tasks()
        if count > 0:
            logger.info(f"Recovered {count} timeout tasks")
    except Exception as e:
        logger.error(f"Failed to recover timeout tasks: {e}")
    finally:
        db.close()


def start_scheduler():
    """Start the background scheduler"""
    global _scheduler
    if _scheduler is None:
        _scheduler = create_scheduler()
        _scheduler.start()
        logger.info("Background scheduler started")


def stop_scheduler():
    """Stop the background scheduler"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown()
        _scheduler = None
        logger.info("Background scheduler stopped")


def get_scheduler() -> BackgroundScheduler:
    """Get the global scheduler instance"""
    global _scheduler
    return _scheduler
