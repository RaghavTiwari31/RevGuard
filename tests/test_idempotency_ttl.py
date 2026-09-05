"""
Tests for idempotency lock expiry and reclaim.

The bug this closes: a lock is claimed *before* triage runs, so a process that
died mid-pipeline — a crash, or the free tier idling the service out — left a
permanent tombstone. The event could never be reprocessed and Razorpay's own
webhook retry was silently swallowed forever.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import IdempotencyLock
from app.guardrails import check_idempotency, complete_idempotency
from app.policy import Policy


def _uid() -> str:
    return f"pay_{uuid.uuid4().hex[:14]}"


def _trace() -> str:
    return f"trc_{uuid.uuid4().hex}"


def _policy(ttl_minutes: float = 15.0) -> Policy:
    return Policy(idempotency_lock_ttl_minutes=ttl_minutes)


class TestLockLifecycle:
    async def test_first_attempt_records_an_expiry(self, async_session: AsyncSession):
        policy = _policy()
        event_id = _uid()
        now = datetime.now(timezone.utc)

        result = await check_idempotency(async_session, event_id, _trace(), policy, now)
        assert result.passed is True

        lock = (
            await async_session.execute(
                select(IdempotencyLock).where(IdempotencyLock.event_id == event_id)
            )
        ).scalars().first()

        assert lock is not None
        assert lock.completed_at is None
        assert lock.expires_at is not None

    async def test_in_flight_attempt_is_refused(self, async_session: AsyncSession):
        policy = _policy()
        event_id = _uid()
        now = datetime.now(timezone.utc)

        assert (await check_idempotency(async_session, event_id, _trace(), policy, now)).passed
        await async_session.commit()

        second = await check_idempotency(async_session, event_id, _trace(), policy, now)
        assert second.passed is False
        assert "being processed" in second.reason

    async def test_completed_lock_is_a_permanent_duplicate(self, async_session: AsyncSession):
        """A finished event stays finished — its lock must never expire."""
        policy = _policy(ttl_minutes=15)
        event_id = _uid()
        now = datetime.now(timezone.utc)

        assert (await check_idempotency(async_session, event_id, _trace(), policy, now)).passed
        await complete_idempotency(async_session, event_id, now)
        await async_session.commit()

        # Long past the TTL, a completed lock is still authoritative.
        much_later = now + timedelta(days=30)
        result = await check_idempotency(async_session, event_id, _trace(), policy, much_later)
        assert result.passed is False
        assert "already processed" in result.reason

    async def test_expired_incomplete_lock_is_reclaimed(self, async_session: AsyncSession):
        """
        The crash-recovery case: an attempt claimed the lock and died. Past the
        TTL a new attempt must be able to take it over, or the event is stuck
        forever.
        """
        policy = _policy(ttl_minutes=15)
        event_id = _uid()
        crashed_at = datetime.now(timezone.utc)

        first_trace = _trace()
        assert (
            await check_idempotency(async_session, event_id, first_trace, policy, crashed_at)
        ).passed
        await async_session.commit()
        # ... process dies here, complete_idempotency is never called ...

        after_ttl = crashed_at + timedelta(minutes=16)
        new_trace = _trace()
        result = await check_idempotency(async_session, event_id, new_trace, policy, after_ttl)

        assert result.passed is True
        assert "Reclaimed" in result.reason

        lock = (
            await async_session.execute(
                select(IdempotencyLock).where(IdempotencyLock.event_id == event_id)
            )
        ).scalars().first()
        assert lock.trace_id == new_trace

    async def test_reclaim_waits_for_the_full_ttl(self, async_session: AsyncSession):
        policy = _policy(ttl_minutes=15)
        event_id = _uid()
        start = datetime.now(timezone.utc)

        assert (await check_idempotency(async_session, event_id, _trace(), policy, start)).passed
        await async_session.commit()

        just_inside = start + timedelta(minutes=14, seconds=59)
        assert (
            await check_idempotency(async_session, event_id, _trace(), policy, just_inside)
        ).passed is False

    async def test_only_one_lock_row_survives_reclaim(self, async_session: AsyncSession):
        """Reclaiming must take over the existing row, never add a second."""
        policy = _policy(ttl_minutes=1)
        event_id = _uid()
        now = datetime.now(timezone.utc)

        await check_idempotency(async_session, event_id, _trace(), policy, now)
        await async_session.commit()

        for minute in (2, 4, 6):
            await check_idempotency(
                async_session, event_id, _trace(), policy, now + timedelta(minutes=minute)
            )
            await async_session.commit()

        rows = (
            await async_session.execute(
                select(IdempotencyLock).where(IdempotencyLock.event_id == event_id)
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_completing_a_missing_lock_is_harmless(self, async_session: AsyncSession):
        await complete_idempotency(async_session, "pay_does_not_exist")
