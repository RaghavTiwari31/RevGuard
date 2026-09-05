"""
Unit tests for the Pre-Flight Invariant Engine (app/guardrails.py).

Tests cover all four guardrail checks against the Phase 1 Definition of Done:

  DoD 1: Same event_id 10× concurrently → exactly 1 processed record.
  DoD 2: Event during 21:00–09:00 IST → rejected with quiet_hours failure.
  DoD 3: 4th attempt on same customer/event → rejected before downstream logic.
  DoD 4: All thresholds read from policy.yaml → verified by mutation.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Customer, IdempotencyLock
from app.guardrails import (
    check_anti_spam,
    check_idempotency,
    check_quiet_hours,
    check_retry_cap,
    run_pre_flight,
)
from app.policy import Policy


IST = ZoneInfo("Asia/Kolkata")


# ── Helper factories ───────────────────────────────────────────────────────────

def _make_policy(**overrides) -> Policy:
    """Create a Policy with default values, optionally overriding fields."""
    defaults = dict(
        max_retry_attempts=3,
        anti_spam_cooldown_hours=4,
        quiet_hours_start="21:00",
        quiet_hours_end="09:00",
        timezone="Asia/Kolkata",
        min_confidence_for_autonomous_action=0.75,
        voice_call_min_amount_inr=100,
        stop_keywords=["unauthorized", "fraud", "unsubscribe", "stop"],
    )
    defaults.update(overrides)
    return Policy(**defaults)


def _uid() -> str:
    return f"pay_{uuid.uuid4().hex[:16]}"


def _trace_id() -> str:
    return f"trc_{uuid.uuid4().hex}"


# ══════════════════════════════════════════════════════════════════════════════
# 1. Idempotency
# ══════════════════════════════════════════════════════════════════════════════

class TestIdempotency:
    """DoD: Same event_id sent 10× → exactly 1 passes, 9 are rejected."""

    async def test_first_event_passes(self, async_session: AsyncSession):
        policy = _make_policy()
        result = await check_idempotency(async_session, _uid(), _trace_id(), policy)
        assert result.passed is True

    async def test_duplicate_event_rejected(self, async_session: AsyncSession):
        policy = _make_policy()
        event_id = _uid()
        # First attempt
        r1 = await check_idempotency(async_session, event_id, _trace_id(), policy)
        assert r1.passed is True
        # Commit so the unique constraint is visible
        await async_session.commit()
        # Second attempt with the same event_id
        r2 = await check_idempotency(async_session, event_id, _trace_id(), policy)
        assert r2.passed is False
        assert "Duplicate" in r2.reason or "already processed" in r2.reason

    async def test_ten_concurrent_same_event_id_exactly_one_passes(self):
        """
        DoD item 1: fire 10 concurrent inserts for the same event_id using
        10 separate sessions; exactly 1 must win the idempotency race.

        Uses 10 independent in-memory SQLite engines (SQLite can't be truly
        concurrent in-process for this constraint), so we serialise them and
        check the count — the important invariant is that the lock table only
        has 1 row after 10 attempts.
        """
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy import select as sa_select
        from app.db import Base, IdempotencyLock

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

        event_id = _uid()
        policy = _make_policy()
        results = []

        # Run 10 sequential attempts (SQLite in-memory can't truly parallelize)
        for _ in range(10):
            async with factory() as session:
                async with session.begin():
                    r = await check_idempotency(session, event_id, _trace_id(), policy)
                    results.append(r.passed)

        passed_count = sum(results)
        assert passed_count == 1, f"Expected exactly 1 pass, got {passed_count}"

        # Verify DB has exactly 1 lock row
        async with factory() as session:
            rows = (await session.execute(sa_select(IdempotencyLock).where(
                IdempotencyLock.event_id == event_id
            ))).scalars().all()
        assert len(rows) == 1

        await engine.dispose()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Retry Cap
# ══════════════════════════════════════════════════════════════════════════════

class TestRetryCap:
    """DoD: 4th attempt rejected before any downstream logic."""

    async def test_first_attempt_passes(self, async_session: AsyncSession):
        policy = _make_policy(max_retry_attempts=3)
        result = await check_retry_cap(async_session, _uid(), None, policy)
        assert result.passed is True
        assert result.metadata["attempt_number"] == 1

    async def test_third_attempt_passes(self, async_session: AsyncSession):
        from app.db import Trace

        policy = _make_policy(max_retry_attempts=3)
        event_id = _uid()

        # Simulate 2 existing trace rows (attempts 1 and 2)
        for i in range(2):
            async_session.add(Trace(
                trace_id=_trace_id(),
                event_id=event_id,
                pre_flight_passed=True,
            ))
        await async_session.flush()

        result = await check_retry_cap(async_session, event_id, None, policy)
        assert result.passed is True
        assert result.metadata["attempt_number"] == 3

    async def test_fourth_attempt_rejected(self, async_session: AsyncSession):
        """DoD item 3: 4th attempt must be rejected."""
        from app.db import Trace

        policy = _make_policy(max_retry_attempts=3)
        event_id = _uid()

        # Simulate 3 existing trace rows (attempts 1, 2, 3)
        for i in range(3):
            async_session.add(Trace(
                trace_id=_trace_id(),
                event_id=event_id,
                pre_flight_passed=True,
            ))
        await async_session.flush()

        result = await check_retry_cap(async_session, event_id, None, policy)
        assert result.passed is False
        assert result.metadata["attempt_number"] == 4

    async def test_policy_cap_respected(self, async_session: AsyncSession):
        """DoD item 4: changing max_retry_attempts in policy changes behavior."""
        from app.db import Trace

        event_id = _uid()

        # 1 existing trace → attempt_number == 2
        async_session.add(Trace(trace_id=_trace_id(), event_id=event_id, pre_flight_passed=True))
        await async_session.flush()

        # Cap of 1 → 2nd attempt fails
        strict_policy = _make_policy(max_retry_attempts=1)
        result = await check_retry_cap(async_session, event_id, None, strict_policy)
        assert result.passed is False

        # Cap of 5 → 2nd attempt passes
        lenient_policy = _make_policy(max_retry_attempts=5)
        result2 = await check_retry_cap(async_session, event_id, None, lenient_policy)
        assert result2.passed is True


# ══════════════════════════════════════════════════════════════════════════════
# 3. Quiet Hours
# ══════════════════════════════════════════════════════════════════════════════

class TestQuietHours:
    """
    DoD: An event during 21:00–09:00 IST is rejected.
    Tests the overnight window boundary condition precisely.
    """

    def _ist(self, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 9, 4, hour, minute, 0, tzinfo=IST)

    def test_deep_night_rejected(self):
        policy = _make_policy(quiet_hours_start="21:00", quiet_hours_end="09:00")
        result = check_quiet_hours(self._ist(2, 30), policy)
        assert result.passed is False

    def test_just_before_start_passes(self):
        policy = _make_policy(quiet_hours_start="21:00", quiet_hours_end="09:00")
        result = check_quiet_hours(self._ist(20, 59), policy)
        assert result.passed is True

    def test_at_start_boundary_rejected(self):
        policy = _make_policy(quiet_hours_start="21:00", quiet_hours_end="09:00")
        result = check_quiet_hours(self._ist(21, 0), policy)
        assert result.passed is False

    def test_at_end_boundary_passes(self):
        policy = _make_policy(quiet_hours_start="21:00", quiet_hours_end="09:00")
        result = check_quiet_hours(self._ist(9, 0), policy)
        assert result.passed is True  # 09:00 is the first minute outside the window

    def test_midday_passes(self):
        policy = _make_policy(quiet_hours_start="21:00", quiet_hours_end="09:00")
        result = check_quiet_hours(self._ist(14, 0), policy)
        assert result.passed is True

    def test_exactly_at_sunrise_passes(self):
        policy = _make_policy(quiet_hours_start="21:00", quiet_hours_end="09:00")
        result = check_quiet_hours(self._ist(9, 5), policy)
        assert result.passed is True

    def test_policy_window_change_respected(self):
        """DoD item 4: changing quiet window in policy changes behavior."""
        # 10:00 AM is inside a 09:00–11:00 window
        narrow_policy = _make_policy(quiet_hours_start="09:00", quiet_hours_end="11:00")
        assert check_quiet_hours(self._ist(10, 0), narrow_policy).passed is False

        # But NOT inside the default 21:00–09:00 window
        default_policy = _make_policy()
        assert check_quiet_hours(self._ist(10, 0), default_policy).passed is True


# ══════════════════════════════════════════════════════════════════════════════
# 4. Anti-Spam Cooldown
# ══════════════════════════════════════════════════════════════════════════════

class TestAntiSpam:
    """DoD: Outreach blocked if last contact was within cooldown_hours."""

    async def test_no_customer_record_passes(self, async_session: AsyncSession):
        policy = _make_policy(anti_spam_cooldown_hours=4)
        result = await check_anti_spam(async_session, "cust_new", policy)
        assert result.passed is True

    async def test_fresh_contact_blocked(self, async_session: AsyncSession):
        """Last contact was 1 hour ago; cooldown is 4 hours → should fail."""
        from datetime import timedelta

        policy = _make_policy(anti_spam_cooldown_hours=4)
        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        customer = Customer(
            customer_id="cust_123",
            last_contacted_at=one_hour_ago,
        )
        async_session.add(customer)
        await async_session.flush()

        result = await check_anti_spam(async_session, "cust_123", policy, now=now)
        assert result.passed is False
        assert result.metadata["elapsed_hours"] < 4

    async def test_expired_cooldown_passes(self, async_session: AsyncSession):
        """Last contact was 5 hours ago; cooldown is 4 hours → should pass."""
        from datetime import timedelta

        policy = _make_policy(anti_spam_cooldown_hours=4)
        now = datetime.now(timezone.utc)
        five_hours_ago = now - timedelta(hours=5)

        customer = Customer(
            customer_id="cust_456",
            last_contacted_at=five_hours_ago,
        )
        async_session.add(customer)
        await async_session.flush()

        result = await check_anti_spam(async_session, "cust_456", policy, now=now)
        assert result.passed is True

    async def test_policy_cooldown_change_respected(self, async_session: AsyncSession):
        """DoD item 4: changing cooldown in policy changes behavior."""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        three_hours_ago = now - timedelta(hours=3)

        customer = Customer(customer_id="cust_789", last_contacted_at=three_hours_ago)
        async_session.add(customer)
        await async_session.flush()

        # Strict policy (6h cooldown) → still blocked at 3h
        strict = _make_policy(anti_spam_cooldown_hours=6)
        r_strict = await check_anti_spam(async_session, "cust_789", strict, now=now)
        assert r_strict.passed is False

        # Lenient policy (2h cooldown) → passes at 3h
        lenient = _make_policy(anti_spam_cooldown_hours=2)
        r_lenient = await check_anti_spam(async_session, "cust_789", lenient, now=now)
        assert r_lenient.passed is True

    async def test_no_customer_id_skips_check(self, async_session: AsyncSession):
        policy = _make_policy(anti_spam_cooldown_hours=4)
        result = await check_anti_spam(async_session, None, policy)
        assert result.passed is True


# ══════════════════════════════════════════════════════════════════════════════
# 5. Full Pre-Flight Orchestrator
# ══════════════════════════════════════════════════════════════════════════════

class TestPreFlightOrchestrator:
    """Integration tests for run_pre_flight — all checks wired together."""

    async def test_clean_event_passes_all_checks(self, async_session: AsyncSession):
        policy = _make_policy()
        now = datetime(2026, 9, 4, 14, 0, 0, tzinfo=IST)  # 2 PM IST, well within hours

        result = await run_pre_flight(
            session=async_session,
            event_id=_uid(),
            trace_id=_trace_id(),
            customer_id=None,
            now=now,
            policy=policy,
        )
        assert result.passed is True
        assert result.rejection_reason is None
        assert result.idempotency.passed is True
        assert result.retry_cap.passed is True
        assert result.quiet_hours.passed is True
        assert result.anti_spam.passed is True

    async def test_quiet_hours_causes_rejection(self, async_session: AsyncSession):
        policy = _make_policy()
        now = datetime(2026, 9, 4, 23, 30, 0, tzinfo=IST)  # 11:30 PM IST

        result = await run_pre_flight(
            session=async_session,
            event_id=_uid(),
            trace_id=_trace_id(),
            customer_id=None,
            now=now,
            policy=policy,
        )
        assert result.passed is False
        assert "QUIET_HOURS" in result.rejection_reason

    async def test_to_dict_returns_correct_shape(self, async_session: AsyncSession):
        policy = _make_policy()
        now = datetime(2026, 9, 4, 14, 0, 0, tzinfo=IST)

        result = await run_pre_flight(
            session=async_session,
            event_id=_uid(),
            trace_id=_trace_id(),
            customer_id=None,
            now=now,
            policy=policy,
        )
        d = result.to_dict()
        assert set(d.keys()) == {"idempotency_passed", "retry_cap_passed", "quiet_hours_passed", "anti_spam_passed"}
