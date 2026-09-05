"""
RevGuard — Issuer Health Radar API

The radar records per-BIN failure counts and trips issuers into extended
backoff during an outage, but nothing ever read that back: `get_bin_stats` was
documented as "used by the dashboard" and had no callers. An outage the system
had already detected, and was already acting on, was invisible.

Exposes:
  GET /issuers        — every tracked BIN, worst health first
  GET /issuers/{bin}  — one BIN in detail
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.db import get_session_factory
from app.issuer_radar import (
    DEFAULT_SPIKE_THRESHOLD,
    DEFAULT_WINDOW_MINUTES,
    IssuerBinStats,
)
from app.logging_config import get_logger

router = APIRouter(tags=["issuers"])
logger = get_logger(__name__)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _aware(dt)
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _serialise(row: IssuerBinStats, now: datetime) -> dict:
    until = _aware(row.extended_backoff_until)
    in_backoff = bool(row.in_extended_backoff) and until is not None and now < until

    # Fraction of the way to tripping the spike threshold, for a progress meter.
    pressure = min(1.0, (row.rolling_failures or 0) / DEFAULT_SPIKE_THRESHOLD)

    if in_backoff:
        health = "outage"
    elif pressure >= 0.5:
        health = "degraded"
    else:
        health = "healthy"

    return {
        "bin": row.bin,
        "health": health,
        "in_extended_backoff": in_backoff,
        "extended_backoff_until": _iso(until) if in_backoff else None,
        "backoff_seconds_remaining": (
            int((until - now).total_seconds()) if in_backoff and until else 0
        ),
        "rolling_failures": row.rolling_failures or 0,
        "spike_threshold": DEFAULT_SPIKE_THRESHOLD,
        "window_minutes": DEFAULT_WINDOW_MINUTES,
        "pressure": round(pressure, 3),
        "total_failures": row.total_failures or 0,
        "last_failure_at": _iso(row.last_failure_at),
        "window_start": _iso(row.window_start),
    }


@router.get("/issuers")
async def list_issuers(
    limit: int = Query(default=50, ge=1, le=200),
    only_unhealthy: bool = Query(default=False, description="Hide healthy issuers"),
):
    """
    Every tracked BIN, ordered worst-first so an active outage is always the
    first thing on screen.
    """
    now = datetime.now(timezone.utc)
    factory = get_session_factory()

    async with factory() as session:
        rows = (
            await session.execute(
                select(IssuerBinStats).order_by(
                    IssuerBinStats.in_extended_backoff.desc(),
                    IssuerBinStats.rolling_failures.desc(),
                    IssuerBinStats.total_failures.desc(),
                )
            )
        ).scalars().all()

    issuers = [_serialise(r, now) for r in rows]
    if only_unhealthy:
        issuers = [i for i in issuers if i["health"] != "healthy"]

    in_outage = sum(1 for i in issuers if i["in_extended_backoff"])

    return {
        "issuers": issuers[:limit],
        "total_tracked": len(rows),
        "in_outage": in_outage,
        "degraded": sum(1 for i in issuers if i["health"] == "degraded"),
        "spike_threshold": DEFAULT_SPIKE_THRESHOLD,
        "window_minutes": DEFAULT_WINDOW_MINUTES,
    }


@router.get("/issuers/{issuer_bin}")
async def get_issuer(issuer_bin: str):
    """Detail for a single BIN."""
    now = datetime.now(timezone.utc)
    factory = get_session_factory()
    bin_clean = issuer_bin[:6].strip()

    async with factory() as session:
        row: Optional[IssuerBinStats] = (
            await session.execute(
                select(IssuerBinStats).where(IssuerBinStats.bin == bin_clean)
            )
        ).scalars().first()

    if row is None:
        raise HTTPException(status_code=404, detail=f"No stats for BIN {bin_clean!r}")

    return _serialise(row, now)
