"""
RevGuard — Retry queue API

Read and control the durable retry queue (`app.retry_queue`).  Makes the
otherwise-invisible middle of the recovery loop inspectable: what is armed,
what came due while the service was asleep, and what failed.

Exposes:
  GET  /retries              — queue contents, soonest first
  POST /retries/{id}/run     — fire a pending retry immediately
  POST /retries/{id}/cancel  — cancel a pending retry
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.db import ScheduledRetry, get_session_factory
from app.logging_config import get_logger
from app.retry_queue import run_retry
from app.scheduler import get_scheduler

router = APIRouter(tags=["retries"])
logger = get_logger(__name__)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _serialise(row: ScheduledRetry, now: datetime) -> dict:
    run_at = row.run_at
    if run_at is not None and run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)

    return {
        "retry_id": row.retry_id,
        "event_id": row.event_id,
        "origin_trace_id": row.origin_trace_id,
        "result_trace_id": row.result_trace_id,
        "status": row.status,
        "attempt_number": row.attempt_number,
        "run_at": _iso(row.run_at),
        "seconds_until_due": (
            int((run_at - now).total_seconds())
            if run_at and row.status == ScheduledRetry.STATUS_PENDING
            else None
        ),
        "overdue": bool(
            run_at and run_at <= now and row.status == ScheduledRetry.STATUS_PENDING
        ),
        "reason": row.reason,
        "last_error": row.last_error,
        "created_at": _iso(row.created_at),
    }


@router.get("/retries")
async def list_retries(
    status: Optional[str] = Query(default=None, description="pending | running | completed | failed | cancelled"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Queue contents, soonest-due first, plus a count by status."""
    now = datetime.now(timezone.utc)
    factory = get_session_factory()

    filters = [ScheduledRetry.status == status] if status else []

    async with factory() as session:
        rows = (
            await session.execute(
                select(ScheduledRetry)
                .where(*filters)
                .order_by(ScheduledRetry.run_at.asc())
                .limit(limit)
            )
        ).scalars().all()

        counts = dict(
            (
                await session.execute(
                    select(ScheduledRetry.status, func.count()).group_by(ScheduledRetry.status)
                )
            ).all()
        )

    # Whether the in-process timer is actually running, so a stalled scheduler
    # is visible rather than presenting as "nothing ever fires".
    try:
        scheduler_running = get_scheduler().running
    except Exception:
        scheduler_running = False

    return {
        "retries": [_serialise(r, now) for r in rows],
        "counts": counts,
        "pending": counts.get(ScheduledRetry.STATUS_PENDING, 0),
        "scheduler_running": scheduler_running,
    }


@router.post("/retries/{retry_id}/run")
async def run_now(retry_id: str):
    """
    Fire a pending retry immediately instead of waiting for its due time.

    Useful for demonstrating the retry path without waiting out a 20-45 minute
    bank-uptime delay.
    """
    factory = get_session_factory()
    async with factory() as session:
        row: Optional[ScheduledRetry] = (
            await session.execute(
                select(ScheduledRetry).where(ScheduledRetry.retry_id == retry_id)
            )
        ).scalars().first()

        if row is None:
            raise HTTPException(status_code=404, detail=f"No retry {retry_id!r}")
        if row.status != ScheduledRetry.STATUS_PENDING:
            raise HTTPException(
                status_code=409,
                detail=f"Retry {retry_id!r} is {row.status}, not pending",
            )

    trace_id = await run_retry(retry_id)
    if trace_id is None:
        raise HTTPException(status_code=500, detail="Retry did not complete — see logs")

    return {"status": "completed", "retry_id": retry_id, "trace_id": trace_id}


@router.post("/retries/{retry_id}/cancel")
async def cancel(retry_id: str):
    """Cancel a pending retry and disarm its timer."""
    factory = get_session_factory()

    async with factory() as session:
        async with session.begin():
            row: Optional[ScheduledRetry] = (
                await session.execute(
                    select(ScheduledRetry).where(ScheduledRetry.retry_id == retry_id)
                )
            ).scalars().first()

            if row is None:
                raise HTTPException(status_code=404, detail=f"No retry {retry_id!r}")
            if row.status != ScheduledRetry.STATUS_PENDING:
                raise HTTPException(
                    status_code=409,
                    detail=f"Retry {retry_id!r} is {row.status}, not pending",
                )

            row.status = ScheduledRetry.STATUS_CANCELLED
            row.reason = "Cancelled via API"

    try:
        scheduler = get_scheduler()
        if scheduler.running:
            scheduler.remove_job(f"revguard_retry_{retry_id}")
    except Exception:
        pass  # Timer may already have fired or never been armed.

    logger.info("retry.cancelled_via_api", extra={"retry_id": retry_id})
    return {"status": "cancelled", "retry_id": retry_id}
