"""
RevGuard — Issuer Health Radar

A rolling per-BIN (bank identification number) failure counter that detects
outages.  When a BIN's failure rate exceeds the threshold within the rolling
window, new TRANSIENT_DOWNTIME events for that issuer are put into extended
backoff instead of the normal retry schedule.

Design:
  - Stored in the DB table `issuer_bin_stats` (durable across restarts)
  - One row per BIN, updated on every failed payment event
  - "Extended backoff" is triggered when rolling_failures >= spike_threshold
    within the last window_minutes

This is a cheap, deterministic anomaly-detection layer — no ML required.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, Integer, String, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.logging_config import get_logger

logger = get_logger(__name__)

# ── Policy defaults (overridable) ─────────────────────────────────────────────
DEFAULT_SPIKE_THRESHOLD = 30        # DoD: 30 failures → extended backoff
DEFAULT_WINDOW_MINUTES = 15         # Rolling window for counting failures
DEFAULT_EXTENDED_BACKOFF_HOURS = 4  # How long to back off when spike detected


# ── DB Model ──────────────────────────────────────────────────────────────────

class IssuerBinStats(Base):
    """Rolling failure stats per bank BIN."""

    __tablename__ = "issuer_bin_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bin = Column(String(16), nullable=False, unique=True, index=True)  # 6-digit BIN

    # Rolling window counters
    rolling_failures = Column(Integer, default=0, nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=True)

    # Outage tracking
    in_extended_backoff = Column(Integer, default=0, nullable=False)  # bool as int for SQLite
    extended_backoff_until = Column(DateTime(timezone=True), nullable=True)

    # Lifetime stats
    total_failures = Column(Integer, default=0, nullable=False)
    last_failure_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)


# ── Radar logic ───────────────────────────────────────────────────────────────

async def record_failure(
    session: AsyncSession,
    issuer_bin: Optional[str],
    spike_threshold: int = DEFAULT_SPIKE_THRESHOLD,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
    extended_backoff_hours: int = DEFAULT_EXTENDED_BACKOFF_HOURS,
    now: Optional[datetime] = None,
) -> bool:
    """
    Record a failure for the given BIN and check if it crosses the spike
    threshold.  Returns True if the BIN is now in extended backoff mode.

    Thread-safe under the assumption of a single Uvicorn worker (as per spec).
    """
    if not issuer_bin:
        return False

    bin_clean = issuer_bin[:6].strip()  # Normalise to first 6 digits
    if not bin_clean:
        return False

    if now is None:
        now = datetime.now(timezone.utc)

    # Fetch or create the BIN row
    stmt = select(IssuerBinStats).where(IssuerBinStats.bin == bin_clean)
    row: Optional[IssuerBinStats] = (await session.execute(stmt)).scalars().first()

    if row is None:
        row = IssuerBinStats(
            bin=bin_clean,
            rolling_failures=0,
            window_start=now,
            in_extended_backoff=0,
            total_failures=0,
        )
        session.add(row)
        await session.flush()

    # Check if we're already in extended backoff
    if row.in_extended_backoff and row.extended_backoff_until:
        until = row.extended_backoff_until
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if now < until:
            # Still in backoff — just increment counters
            row.total_failures += 1
            row.last_failure_at = now
            row.updated_at = now
            logger.info("issuer_radar.still_in_backoff", extra={
                "bin": bin_clean,
                "backoff_until": until.isoformat(),
            })
            return True

        # Backoff expired — reset
        row.in_extended_backoff = 0
        row.extended_backoff_until = None
        row.rolling_failures = 0
        row.window_start = now

    # Reset rolling window if it has expired
    window_start = row.window_start
    if window_start:
        if window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=timezone.utc)
        if now - window_start > timedelta(minutes=window_minutes):
            row.rolling_failures = 0
            row.window_start = now

    # Increment counters
    row.rolling_failures += 1
    row.total_failures += 1
    row.last_failure_at = now
    row.updated_at = now

    logger.info("issuer_radar.failure_recorded", extra={
        "bin": bin_clean,
        "rolling_failures": row.rolling_failures,
        "spike_threshold": spike_threshold,
        "window_minutes": window_minutes,
    })

    # Check for spike
    if row.rolling_failures >= spike_threshold:
        row.in_extended_backoff = 1
        row.extended_backoff_until = now + timedelta(hours=extended_backoff_hours)
        row.rolling_failures = 0
        row.window_start = now

        logger.warning("issuer_radar.spike_detected", extra={
            "bin": bin_clean,
            "total_failures_in_window": row.rolling_failures + spike_threshold,
            "extended_backoff_until": row.extended_backoff_until.isoformat(),
        })
        return True

    return False


async def is_in_extended_backoff(
    session: AsyncSession,
    issuer_bin: Optional[str],
    now: Optional[datetime] = None,
) -> bool:
    """
    Check if the given BIN is currently in extended backoff mode.
    Pure read — does not mutate state.
    """
    if not issuer_bin:
        return False

    bin_clean = issuer_bin[:6].strip()
    if now is None:
        now = datetime.now(timezone.utc)

    stmt = select(IssuerBinStats).where(IssuerBinStats.bin == bin_clean)
    row: Optional[IssuerBinStats] = (await session.execute(stmt)).scalars().first()

    if row is None or not row.in_extended_backoff:
        return False

    until = row.extended_backoff_until
    if until is None:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    return now < until


async def get_bin_stats(
    session: AsyncSession,
    issuer_bin: str,
) -> Optional[IssuerBinStats]:
    """Return raw stats for a BIN (used by the dashboard)."""
    bin_clean = issuer_bin[:6].strip()
    stmt = select(IssuerBinStats).where(IssuerBinStats.bin == bin_clean)
    return (await session.execute(stmt)).scalars().first()
