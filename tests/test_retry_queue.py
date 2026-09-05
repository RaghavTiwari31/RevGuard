"""
Tests for the durable retry queue.

Strategy 1 previously scheduled a job that only logged — nothing was ever
retried. These tests pin down that retries are real, that they survive a
restart, and (most importantly) that they terminate rather than looping.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db as db_module
from app.db import Base, Event, ScheduledRetry, Trace
from app.policy import Policy
from app.retry_queue import (
    cancel_retries_for_event,
    rehydrate,
    run_retry,
    schedule_retry,
)


def _event_id() -> str:
    return f"pay_{uuid.uuid4().hex[:14]}"


def _raw_payload(event_id: str, error_code: str = "gateway_timeout") -> str:
    return json.dumps({
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": event_id,
            "amount": 250000,
            "error_code": error_code,
            "contact": "+919000000000",
            "email": "customer@example.com",
            "card_id": "411111000000",
        }}},
    })


async def _seed_event(session: AsyncSession, event_id: str, error_code: str = "gateway_timeout") -> Event:
    event = Event(
        event_id=event_id,
        payment_id=event_id,
        customer_id=f"cust_{uuid.uuid4().hex[:8]}",
        amount_paise=250000,
        currency="INR",
        error_code=error_code,
        bank="HDFC Bank",
        issuer_bin="411111",
        raw_payload=_raw_payload(event_id, error_code),
    )
    session.add(event)
    await session.flush()
    return event


@pytest.fixture
async def live_db():
    """
    A real session factory installed on the db module.

    The retry queue opens its own sessions (it runs from a timer, not a
    request), so it needs the module-level factory rather than an injected one.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    prev_engine, prev_factory = db_module._engine, db_module._session_factory
    db_module._engine, db_module._session_factory = engine, factory

    yield factory

    db_module._engine, db_module._session_factory = prev_engine, prev_factory
    await engine.dispose()


class TestScheduling:
    async def test_retry_is_persisted(self, live_db):
        event_id = _event_id()
        run_at = datetime.now(timezone.utc) + timedelta(minutes=20)

        async with live_db() as session:
            async with session.begin():
                await _seed_event(session, event_id)
                row = await schedule_retry(
                    session, event_id, run_at, attempt_number=2, reason="transient"
                )
                assert row is not None
                retry_id = row.retry_id

        async with live_db() as session:
            stored = (
                await session.execute(
                    select(ScheduledRetry).where(ScheduledRetry.retry_id == retry_id)
                )
            ).scalars().first()

        assert stored is not None
        assert stored.status == ScheduledRetry.STATUS_PENDING
        assert stored.attempt_number == 2

    async def test_policy_can_disable_retries(self, live_db):
        policy = Policy(enable_scheduled_retries=False)
        async with live_db() as session:
            async with session.begin():
                await _seed_event(session, _event_id())
                row = await schedule_retry(
                    session, _event_id(), datetime.now(timezone.utc), 2, policy=policy
                )
        assert row is None

    async def test_delay_is_clamped_to_the_ceiling(self, live_db):
        """A bad delay must not park a retry a year into the future."""
        policy = Policy(max_retry_delay_minutes=60)
        far_future = datetime.now(timezone.utc) + timedelta(days=365)

        async with live_db() as session:
            async with session.begin():
                await _seed_event(session, _event_id())
                row = await schedule_retry(
                    session, _event_id(), far_future, 2, policy=policy
                )

        assert row is not None
        assert row.run_at <= datetime.now(timezone.utc) + timedelta(minutes=61)


class TestExecution:
    async def test_retry_replays_triage_and_writes_a_trace(self, live_db):
        event_id = _event_id()

        async with live_db() as session:
            async with session.begin():
                await _seed_event(session, event_id)
                row = await schedule_retry(
                    session, event_id, datetime.now(timezone.utc), attempt_number=2
                )
                retry_id = row.retry_id

        trace_id = await run_retry(retry_id)
        assert trace_id is not None

        async with live_db() as session:
            trace = (
                await session.execute(select(Trace).where(Trace.trace_id == trace_id))
            ).scalars().first()
            done = (
                await session.execute(
                    select(ScheduledRetry).where(ScheduledRetry.retry_id == retry_id)
                )
            ).scalars().first()
            event = (
                await session.execute(select(Event).where(Event.event_id == event_id))
            ).scalars().first()

        assert trace is not None
        assert trace.event_id == event_id
        assert trace.category == "TRANSIENT_DOWNTIME"
        assert done.status == ScheduledRetry.STATUS_COMPLETED
        assert done.result_trace_id == trace_id
        assert event.retry_count == 2

    async def test_retry_runs_only_once(self, live_db):
        """A duplicate timer firing must not double-process."""
        event_id = _event_id()
        async with live_db() as session:
            async with session.begin():
                await _seed_event(session, event_id)
                retry_id = (
                    await schedule_retry(session, event_id, datetime.now(timezone.utc), 2)
                ).retry_id

        assert await run_retry(retry_id) is not None
        assert await run_retry(retry_id) is None

    async def test_cancelled_retry_does_not_run(self, live_db):
        event_id = _event_id()
        async with live_db() as session:
            async with session.begin():
                await _seed_event(session, event_id)
                retry_id = (
                    await schedule_retry(session, event_id, datetime.now(timezone.utc), 2)
                ).retry_id

        async with live_db() as session:
            async with session.begin():
                await cancel_retries_for_event(session, event_id, "customer disputed")

        assert await run_retry(retry_id) is None

    async def test_missing_event_marks_the_retry_failed(self, live_db):
        """A retry for an event that is gone must fail loudly, not hang pending."""
        async with live_db() as session:
            async with session.begin():
                retry_id = (
                    await schedule_retry(
                        session, "pay_never_stored", datetime.now(timezone.utc), 2
                    )
                ).retry_id

        assert await run_retry(retry_id) is None

        async with live_db() as session:
            row = (
                await session.execute(
                    select(ScheduledRetry).where(ScheduledRetry.retry_id == retry_id)
                )
            ).scalars().first()

        assert row.status == ScheduledRetry.STATUS_FAILED
        assert row.last_error

    async def test_retry_chain_terminates_at_the_cap(self, live_db):
        """
        The safety property that matters most: retries must not loop forever.
        Each replay re-enters the pipeline, so the retry cap has to convert the
        chain into a circuit-breaker escalation.
        """
        event_id = _event_id()
        async with live_db() as session:
            async with session.begin():
                await _seed_event(session, event_id)
                retry_id = (
                    await schedule_retry(session, event_id, datetime.now(timezone.utc), 2)
                ).retry_id

        actions = []
        for _ in range(10):
            trace_id = await run_retry(retry_id)
            if trace_id is None:
                break
            async with live_db() as session:
                trace = (
                    await session.execute(select(Trace).where(Trace.trace_id == trace_id))
                ).scalars().first()
                actions.append(trace.action_type)

                nxt = (
                    await session.execute(
                        select(ScheduledRetry).where(
                            ScheduledRetry.event_id == event_id,
                            ScheduledRetry.status == ScheduledRetry.STATUS_PENDING,
                        )
                    )
                ).scalars().first()
            if nxt is None:
                break
            retry_id = nxt.retry_id

        assert "ESCALATED_HUMAN_ATTENTION" in actions, actions
        assert len(actions) <= 5, f"retry chain did not terminate promptly: {actions}"

        async with live_db() as session:
            still_pending = (
                await session.execute(
                    select(ScheduledRetry).where(
                        ScheduledRetry.event_id == event_id,
                        ScheduledRetry.status == ScheduledRetry.STATUS_PENDING,
                    )
                )
            ).scalars().all()
        assert still_pending == []


class TestRehydration:
    async def test_pending_retries_are_reloaded_after_a_restart(self, live_db):
        """
        The free-tier case: the process idles out with retries pending. They
        must come back, including ones that came due while it was asleep.
        """
        now = datetime.now(timezone.utc)

        async with live_db() as session:
            async with session.begin():
                for offset in (-90, -30, 30, 90):  # two overdue, two future
                    event_id = _event_id()
                    await _seed_event(session, event_id)
                    await schedule_retry(
                        session, event_id, now + timedelta(minutes=offset), 2
                    )

        result = await rehydrate()

        assert result["caught_up"] == 2, "overdue retries must be caught up, not dropped"
        assert result["rearmed"] == 2

    async def test_rehydrate_ignores_settled_retries(self, live_db):
        now = datetime.now(timezone.utc)
        async with live_db() as session:
            async with session.begin():
                event_id = _event_id()
                await _seed_event(session, event_id)
                row = await schedule_retry(session, event_id, now, 2)
                row.status = ScheduledRetry.STATUS_COMPLETED

        result = await rehydrate()
        assert result["rearmed"] + result["caught_up"] == 0

    async def test_rehydrate_is_a_noop_when_retries_are_disabled(self, live_db):
        result = await rehydrate(Policy(enable_scheduled_retries=False))
        assert result == {"rearmed": 0, "caught_up": 0}
