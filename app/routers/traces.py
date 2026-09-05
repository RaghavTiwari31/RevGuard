"""
RevGuard — Trace history API

Traces were being written to the database and never read back.  The dashboard
was driven purely by the live SSE stream, so a page refresh — or a judge opening
the URL after a batch had already run — showed an empty table with no way to
recover the history that was sitting in the database the whole time.

Exposes:
  GET /traces             — paginated, filterable history (newest first)
  GET /traces/{trace_id}  — one trace in full
  GET /traces/stats       — aggregate rollup over the stored history
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from app.db import Trace, get_session_factory
from app.logging_config import get_logger

router = APIRouter(tags=["traces"])
logger = get_logger(__name__)

# Cap the page size: the free tier runs a single small worker, and an unbounded
# limit is an easy way for one request to exhaust it.
MAX_LIMIT = 200


def _loads(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _serialise(trace: Trace) -> dict:
    """
    Shape a stored Trace like a live SSE `trace_update`.

    Keeping the two identical is deliberate: the dashboard can drop a
    historical row and a live row into the same table without special-casing
    either one.
    """
    return {
        "type": "trace_update",
        "trace_id": trace.trace_id,
        "event_id": trace.event_id,
        "category": trace.category,
        "action_type": trace.action_type,
        "outcome_status": trace.outcome_status,
        "amount_inr": trace.amount_inr,
        "confidence": trace.confidence_score,
        "rationale": trace.rationale,
        "hinglish_message": trace.hinglish_message,
        "provider_used": trace.llm_provider,
        "dispatch_channel": trace.dispatch_channel,
        "dispatch_cost_inr": trace.dispatch_cost_inr,
        "razorpay_link_url": trace.razorpay_link_url,
        "classification_rule": trace.classification_rule,
        "guardrail_checks": _loads(trace.guardrail_checks),
        "metadata": _loads(trace.triage_metadata),
        "pre_flight_passed": trace.pre_flight_passed,
        "pre_flight_rejection_reason": trace.pre_flight_rejection_reason,
        "retry_scheduled_at": _iso(trace.retry_scheduled_at),
        "timestamp": _iso(trace.created_at),
        "source": "history",
    }


@router.get("/traces")
async def list_traces(
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    category: Optional[str] = Query(default=None, description="Filter by failure category"),
    action_type: Optional[str] = Query(default=None, description="Filter by recovery action"),
    outcome_status: Optional[str] = Query(default=None, description="Filter by outcome"),
    event_id: Optional[str] = Query(default=None, description="All attempts for one event"),
    search: Optional[str] = Query(default=None, description="Substring match on event id"),
):
    """
    Paginated trace history, newest first.

    Returns `total` alongside the page so the dashboard can show how much
    history exists without walking every page.
    """
    factory = get_session_factory()

    filters = []
    if category:
        filters.append(Trace.category == category)
    if action_type:
        filters.append(Trace.action_type == action_type)
    if outcome_status:
        filters.append(Trace.outcome_status == outcome_status)
    if event_id:
        filters.append(Trace.event_id == event_id)
    if search:
        filters.append(Trace.event_id.ilike(f"%{search}%"))

    async with factory() as session:
        total = (
            await session.execute(
                select(func.count()).select_from(Trace).where(*filters)
            )
        ).scalar_one()

        rows = (
            await session.execute(
                select(Trace)
                .where(*filters)
                .order_by(Trace.created_at.desc(), Trace.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "traces": [_serialise(t) for t in rows],
    }


@router.get("/traces/stats")
async def trace_stats():
    """
    Aggregate rollup over all stored traces.

    Computed in SQL rather than by pulling rows into Python — on a free-tier
    worker the difference between a GROUP BY and loading every trace into
    memory is the difference between working and falling over.
    """
    factory = get_session_factory()

    async with factory() as session:
        total = (await session.execute(select(func.count()).select_from(Trace))).scalar_one()

        by_category = (
            await session.execute(
                select(Trace.category, func.count(), func.sum(Trace.amount_inr))
                .where(Trace.category.is_not(None))
                .group_by(Trace.category)
            )
        ).all()

        by_action = (
            await session.execute(
                select(Trace.action_type, func.count())
                .where(Trace.action_type.is_not(None))
                .group_by(Trace.action_type)
            )
        ).all()

        by_channel = (
            await session.execute(
                select(Trace.dispatch_channel, func.count(), func.sum(Trace.dispatch_cost_inr))
                .where(Trace.dispatch_channel.is_not(None))
                .group_by(Trace.dispatch_channel)
            )
        ).all()

        totals = (
            await session.execute(
                select(func.sum(Trace.amount_inr), func.sum(Trace.dispatch_cost_inr))
            )
        ).one()

        rejected = (
            await session.execute(
                select(func.count())
                .select_from(Trace)
                .where(Trace.pre_flight_passed.is_(False))
            )
        ).scalar_one()

    return {
        "total_traces": total,
        "pre_flight_rejected": rejected,
        "total_amount_inr": round(totals[0] or 0.0, 2),
        "total_dispatch_cost_inr": round(totals[1] or 0.0, 2),
        "by_category": [
            {"category": c, "count": n, "amount_inr": round(amt or 0.0, 2)}
            for c, n, amt in by_category
        ],
        "by_action": [{"action_type": a, "count": n} for a, n in by_action],
        "by_channel": [
            {"channel": ch, "count": n, "cost_inr": round(cost or 0.0, 2)}
            for ch, n, cost in by_channel
        ],
    }


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str):
    """One trace in full, including every attempt recorded for its event."""
    factory = get_session_factory()

    async with factory() as session:
        trace: Optional[Trace] = (
            await session.execute(select(Trace).where(Trace.trace_id == trace_id))
        ).scalars().first()

        if trace is None:
            raise HTTPException(status_code=404, detail=f"No trace {trace_id!r}")

        siblings = (
            await session.execute(
                select(Trace)
                .where(Trace.event_id == trace.event_id)
                .order_by(Trace.created_at.asc(), Trace.id.asc())
            )
        ).scalars().all()

    return {
        "trace": _serialise(trace),
        "attempts": [_serialise(t) for t in siblings],
    }
