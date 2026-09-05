"""
RevGuard — APScheduler setup (in-process delayed retries)

Creates and manages a single AsyncIOScheduler instance for the application
lifetime.  Used by Strategy 1 (silent delayed retry) to schedule payment
retries during optimal banking hours.

Design choices (from the implementation plan):
  - In-process APScheduler — no Redis or Celery needed
  - Single Uvicorn worker — scheduler is the only instance
  - Jobs are lightweight: they just update the Event.retry_count and re-queue
    the event into the triage pipeline
"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Return the singleton scheduler, creating it if needed."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    return _scheduler


def start_scheduler() -> AsyncIOScheduler:
    """Start the scheduler. Call once at app startup."""
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
    return scheduler


def stop_scheduler() -> None:
    """Stop the scheduler gracefully. Call at app shutdown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
