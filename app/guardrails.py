"""
RevGuard — Pre-Flight Invariant Engine

Four deterministic guardrail checks, each reading thresholds from policy.yaml
(never hardcoded).  All checks are pure functions — no I/O — except for the
idempotency lock (which touches the DB) and the retry-cap check (which reads
the Event table).

Check order:
  1. Idempotency Lock     — have we already processed this event_id?
  2. Retry Cap            — has this customer/event exceeded max_retry_attempts?
  3. Quiet Hours          — is it within TRAI-mandated no-outreach hours?
  4. Anti-Spam Cooldown   — was the last outreach too recent?

Every check logs a structured JSON audit line so the decision is traceable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Customer, Event, IdempotencyLock, Trace
from app.logging_config import get_logger
from app.policy import Policy, get_policy

logger = get_logger(__name__)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class GuardrailResult:
    """Outcome of a single guardrail check."""
    passed: bool
    check_name: str
    reason: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class PreFlightResult:
    """Aggregated outcome of all four pre-flight checks."""
    passed: bool
    idempotency: GuardrailResult
    retry_cap: GuardrailResult
    quiet_hours: GuardrailResult
    anti_spam: GuardrailResult
    rejection_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "idempotency_passed": self.idempotency.passed,
            "retry_cap_passed": self.retry_cap.passed,
            "quiet_hours_passed": self.quiet_hours.passed,
            "anti_spam_passed": self.anti_spam.passed,
        }


# ── Individual guardrail checks ───────────────────────────────────────────────

async def check_idempotency(
    session: AsyncSession,
    event_id: str,
    trace_id: str,
    policy: Policy,
) -> GuardrailResult:
    """
    Attempt to INSERT a row into idempotency_locks.
    - Success → this is the first (and only) processor for this event.
    - IntegrityError → a duplicate; reject.

    Using a DB-level UNIQUE constraint gives us a correct, atomic lock even
    under concurrent duplicate webhook deliveries.
    """
    lock = IdempotencyLock(event_id=event_id, trace_id=trace_id)
    try:
        # Use a nested savepoint so an IntegrityError only rolls back the inner
        # block, leaving the outer transaction intact for subsequent checks.
        async with session.begin_nested():
            session.add(lock)
            await session.flush()  # Raises IntegrityError immediately if duplicate
        result = GuardrailResult(
            passed=True,
            check_name="idempotency",
            reason="First processing of this event_id",
        )
    except IntegrityError:
        # Savepoint was already rolled back by the context manager — outer tx ok
        result = GuardrailResult(
            passed=False,
            check_name="idempotency",
            reason=f"Duplicate event_id={event_id!r} — already processed",
        )

    logger.info(
        "guardrail.idempotency",
        extra={"event_id": event_id, "passed": result.passed, "reason": result.reason},
    )
    return result


async def check_retry_cap(
    session: AsyncSession,
    event_id: str,
    customer_id: Optional[str],
    policy: Policy,
) -> GuardrailResult:
    """
    Count how many traces already exist for this event_id.
    If the count ≥ max_retry_attempts, reject (circuit-breaker).

    We count traces rather than the Event.retry_count field so the check is
    correct even if the Event row hasn't been updated yet in this transaction.
    """
    stmt = select(Trace).where(Trace.event_id == event_id)
    existing: list[Trace] = (await session.execute(stmt)).scalars().all()
    attempt_number = len(existing) + 1  # This run would be attempt N

    cap = policy.max_retry_attempts
    passed = attempt_number <= cap

    result = GuardrailResult(
        passed=passed,
        check_name="retry_cap",
        reason=(
            f"Attempt {attempt_number} of {cap} — within cap"
            if passed
            else f"Attempt {attempt_number} exceeds cap of {cap} — circuit-breaker triggered"
        ),
        metadata={"attempt_number": attempt_number, "cap": cap},
    )
    logger.info(
        "guardrail.retry_cap",
        extra={
            "event_id": event_id,
            "customer_id": customer_id,
            "attempt_number": attempt_number,
            "cap": cap,
            "passed": passed,
        },
    )
    return result


def check_quiet_hours(
    now: Optional[datetime],
    policy: Policy,
) -> GuardrailResult:
    """
    TRAI compliance: no outreach between quiet_hours_start and quiet_hours_end
    in the policy timezone (default: Asia/Kolkata).

    Pure function — takes `now` as a parameter so tests can inject arbitrary
    timestamps without monkey-patching.

    Logic handles the overnight window correctly:
      - If start > end (e.g. 21:00 → 09:00): quiet if time >= start OR time < end.
      - If start < end (e.g. 02:00 → 06:00): quiet if start <= time < end.
    """
    tz = ZoneInfo(policy.timezone)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    def _parse(hhmm: str):
        h, m = hhmm.split(":")
        return int(h), int(m)

    sh, sm = _parse(policy.quiet_hours_start)
    eh, em = _parse(policy.quiet_hours_end)

    current_minutes = now.hour * 60 + now.minute
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em

    if start_minutes > end_minutes:
        # Overnight window (e.g. 21:00 → 09:00)
        in_quiet = current_minutes >= start_minutes or current_minutes < end_minutes
    else:
        in_quiet = start_minutes <= current_minutes < end_minutes

    passed = not in_quiet

    result = GuardrailResult(
        passed=passed,
        check_name="quiet_hours",
        reason=(
            f"Outside quiet window ({policy.quiet_hours_start}–{policy.quiet_hours_end} {policy.timezone})"
            if passed
            else f"Inside quiet window ({policy.quiet_hours_start}–{policy.quiet_hours_end} {policy.timezone}) at {now.strftime('%H:%M')} — defer to sunrise"
        ),
        metadata={
            "current_time_local": now.strftime("%H:%M"),
            "quiet_start": policy.quiet_hours_start,
            "quiet_end": policy.quiet_hours_end,
            "timezone": policy.timezone,
        },
    )
    logger.info(
        "guardrail.quiet_hours",
        extra={"passed": passed, "reason": result.reason},
    )
    return result


async def check_anti_spam(
    session: AsyncSession,
    customer_id: Optional[str],
    policy: Policy,
    now: Optional[datetime] = None,
) -> GuardrailResult:
    """
    Reject if the customer was contacted within the cooldown window.
    Reads the Customer.last_contacted_at field.
    If no customer record exists, the check passes (first contact).
    """
    if not customer_id:
        result = GuardrailResult(
            passed=True,
            check_name="anti_spam",
            reason="No customer_id — skipping cooldown check",
        )
        logger.info("guardrail.anti_spam", extra={"passed": True, "reason": result.reason})
        return result

    tz = ZoneInfo(policy.timezone)
    if now is None:
        now = datetime.now(timezone.utc)

    stmt = select(Customer).where(Customer.customer_id == customer_id)
    customer: Optional[Customer] = (await session.execute(stmt)).scalars().first()

    if customer is None or customer.last_contacted_at is None:
        result = GuardrailResult(
            passed=True,
            check_name="anti_spam",
            reason="No prior contact on record for this customer",
        )
    else:
        last = customer.last_contacted_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed_hours = (now - last).total_seconds() / 3600
        cooldown = policy.anti_spam_cooldown_hours
        passed = elapsed_hours >= cooldown
        result = GuardrailResult(
            passed=passed,
            check_name="anti_spam",
            reason=(
                f"Cooldown elapsed: {elapsed_hours:.1f}h ≥ {cooldown}h"
                if passed
                else f"Still within cooldown: {elapsed_hours:.1f}h < {cooldown}h — suppressing outreach"
            ),
            metadata={
                "elapsed_hours": round(elapsed_hours, 2),
                "cooldown_hours": cooldown,
                "last_contacted_at": last.isoformat(),
            },
        )

    logger.info(
        "guardrail.anti_spam",
        extra={
            "customer_id": customer_id,
            "passed": result.passed,
            "reason": result.reason,
            **result.metadata,
        },
    )
    return result


# ── Orchestrator ──────────────────────────────────────────────────────────────

async def run_pre_flight(
    session: AsyncSession,
    event_id: str,
    trace_id: str,
    customer_id: Optional[str],
    now: Optional[datetime] = None,
    policy: Optional[Policy] = None,
) -> PreFlightResult:
    """
    Run all four guardrail checks in order.
    Short-circuits after the first failure — subsequent checks are still run
    (and logged) but the overall result is already failed, ensuring a complete
    audit trail.

    Returns a PreFlightResult.  The caller decides what HTTP status to return.
    """
    if policy is None:
        policy = get_policy()

    idempotency = await check_idempotency(session, event_id, trace_id, policy)
    retry_cap = await check_retry_cap(session, event_id, customer_id, policy)
    quiet_hours = check_quiet_hours(now, policy)
    anti_spam = await check_anti_spam(session, customer_id, policy, now)

    all_passed = all([
        idempotency.passed,
        retry_cap.passed,
        quiet_hours.passed,
        anti_spam.passed,
    ])

    rejection_reason: Optional[str] = None
    if not all_passed:
        for check in [idempotency, retry_cap, quiet_hours, anti_spam]:
            if not check.passed:
                rejection_reason = f"{check.check_name.upper()}: {check.reason}"
                break

    result = PreFlightResult(
        passed=all_passed,
        idempotency=idempotency,
        retry_cap=retry_cap,
        quiet_hours=quiet_hours,
        anti_spam=anti_spam,
        rejection_reason=rejection_reason,
    )

    logger.info(
        "preflight.result",
        extra={
            "event_id": event_id,
            "trace_id": trace_id,
            "passed": all_passed,
            "rejection_reason": rejection_reason,
            "checks": result.to_dict(),
        },
    )
    return result
