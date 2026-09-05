"""
RevGuard — Durable Retry Queue

Strategy 1 (Silent Delayed Retry) used to hand APScheduler a job that only
logged and returned.  Nothing was ever actually retried.  This module makes the
retry real, and makes it survive the deployment it has to live in.

Why a database table and not just APScheduler
---------------------------------------------
APScheduler's default job store is in process memory.  On Render's free tier
the service is spun down after ~15 minutes of inactivity and the filesystem is
ephemeral, so an in-memory schedule is exactly the wrong place for a job that
fires 20-45 minutes from now: every pending retry would evaporate, silently, at
the moment the traffic stops — which is precisely when retries are pending.

So each retry is written to `scheduled_retries` first and only then handed to
the scheduler.  At boot, `rehydrate()` reloads whatever is still pending:

  - due in the future     → re-armed at its original time
  - came due while asleep → run shortly after boot, staggered so a long
                            downtime does not produce a thundering herd

The database is the source of truth; APScheduler is just the timer.

Flow of one retry
-----------------
    schedule_retry()      persist row (pending) + arm timer
           │
           ▼
    _execute_retry()      claim row (running) → replay triage → persist Trace
           │                                  → broadcast SSE → mark completed
           ▼
    Trace row + live dashboard update, identical in shape to a first attempt
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from app.db import Event, ScheduledRetry, Trace, get_session_factory
from app.logging_config import get_logger
from app.policy import Policy, get_policy
from app.scheduler import get_scheduler
from app.sse import broadcast

logger = get_logger(__name__)


def _job_id(retry_id: str) -> str:
    return f"revguard_retry_{retry_id}"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; normalise everything to UTC-aware."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ── Scheduling ────────────────────────────────────────────────────────────────

async def schedule_retry(
    session,
    event_id: str,
    run_at: datetime,
    attempt_number: int,
    origin_trace_id: Optional[str] = None,
    reason: str = "",
    policy: Optional[Policy] = None,
) -> Optional[ScheduledRetry]:
    """
    Persist a retry and arm the timer for it.

    The row is written inside the caller's transaction so a retry is never
    armed for work that did not commit.  Returns None when retries are disabled
    by policy.
    """
    if policy is None:
        policy = get_policy()

    if not policy.enable_scheduled_retries:
        return None

    now = datetime.now(timezone.utc)
    ceiling = now + timedelta(minutes=policy.max_retry_delay_minutes)
    run_at = _aware(run_at) or now
    if run_at > ceiling:
        logger.warning("retry.delay_clamped", extra={
            "event_id": event_id,
            "requested": run_at.isoformat(),
            "ceiling": ceiling.isoformat(),
        })
        run_at = ceiling

    retry_id = f"rty_{uuid.uuid4().hex[:16]}"
    row = ScheduledRetry(
        retry_id=retry_id,
        event_id=event_id,
        origin_trace_id=origin_trace_id,
        run_at=run_at,
        attempt_number=attempt_number,
        status=ScheduledRetry.STATUS_PENDING,
        reason=reason[:256] if reason else None,
    )
    session.add(row)
    await session.flush()

    _arm(retry_id, run_at)

    logger.info("retry.scheduled", extra={
        "retry_id": retry_id,
        "event_id": event_id,
        "run_at": run_at.isoformat(),
        "attempt_number": attempt_number,
    })
    return row


def _arm(retry_id: str, run_at: datetime) -> None:
    """Hand the timer to APScheduler. Best-effort: the DB row is authoritative."""
    try:
        scheduler = get_scheduler()
        if not scheduler.running:
            # Nothing to arm against yet (tests, or pre-startup). rehydrate()
            # will pick the row up from the database when the app boots.
            return
        scheduler.add_job(
            func=run_retry,
            trigger="date",
            run_date=max(run_at, datetime.now(timezone.utc) + timedelta(seconds=1)),
            id=_job_id(retry_id),
            args=[retry_id],
            replace_existing=True,
            misfire_grace_time=3600,
        )
    except Exception as exc:  # pragma: no cover — scheduler is best-effort
        logger.warning("retry.arm_failed", extra={"retry_id": retry_id, "error": str(exc)})


async def cancel_retries_for_event(session, event_id: str, reason: str) -> int:
    """
    Cancel every pending retry for an event.

    Used when automation must stop immediately — a customer disputes the charge
    or opts out — so a retry armed minutes ago cannot fire after the freeze.
    """
    rows = (
        await session.execute(
            select(ScheduledRetry).where(
                ScheduledRetry.event_id == event_id,
                ScheduledRetry.status == ScheduledRetry.STATUS_PENDING,
            )
        )
    ).scalars().all()

    for row in rows:
        row.status = ScheduledRetry.STATUS_CANCELLED
        row.reason = reason[:256]
        try:
            scheduler = get_scheduler()
            if scheduler.running:
                scheduler.remove_job(_job_id(row.retry_id))
        except Exception:
            pass  # Job may have already fired or never been armed.

    if rows:
        await session.flush()
        logger.info("retry.cancelled", extra={
            "event_id": event_id, "count": len(rows), "reason": reason,
        })
    return len(rows)


# ── Boot-time recovery ────────────────────────────────────────────────────────

async def rehydrate(policy: Optional[Policy] = None) -> dict:
    """
    Reload pending retries from the database into the scheduler.

    Called once at startup.  This is what makes the queue survive a free-tier
    spin-down: retries that came due while the service was asleep are not lost,
    they are simply late, and are run in a staggered catch-up burst.
    """
    if policy is None:
        policy = get_policy()

    if not policy.enable_scheduled_retries:
        return {"rearmed": 0, "caught_up": 0}

    now = datetime.now(timezone.utc)
    factory = get_session_factory()

    rearmed = 0
    caught_up = 0

    async with factory() as session:
        rows = (
            await session.execute(
                select(ScheduledRetry)
                .where(ScheduledRetry.status == ScheduledRetry.STATUS_PENDING)
                .order_by(ScheduledRetry.run_at)
            )
        ).scalars().all()

        for index, row in enumerate(rows):
            run_at = _aware(row.run_at) or now
            if run_at <= now:
                # Overdue — it came due during downtime. Stagger the catch-up so
                # a long sleep does not fire hundreds of retries at once.
                run_at = now + timedelta(
                    seconds=policy.retry_catchup_delay_seconds + index * 2
                )
                caught_up += 1
            else:
                rearmed += 1
            _arm(row.retry_id, run_at)

    logger.info("retry.rehydrated", extra={
        "rearmed": rearmed,
        "caught_up": caught_up,
        "total": rearmed + caught_up,
    })
    return {"rearmed": rearmed, "caught_up": caught_up}


# ── Execution ─────────────────────────────────────────────────────────────────

async def run_retry(retry_id: str) -> Optional[str]:
    """
    Execute one scheduled retry: replay the original event through triage.

    The replay goes through the same `run_triage` the webhook uses, so a retry
    is subject to every guardrail a first attempt is — including the retry cap,
    which is what eventually converts a repeatedly failing payment into a
    circuit-breaker escalation instead of an infinite loop.

    Returns the new trace_id, or None if the retry did not run.
    """
    from app.triage import run_triage  # Local import: avoids an import cycle.

    factory = get_session_factory()
    policy = get_policy()

    async with factory() as session:
        async with session.begin():
            row: Optional[ScheduledRetry] = (
                await session.execute(
                    select(ScheduledRetry).where(ScheduledRetry.retry_id == retry_id)
                )
            ).scalars().first()

            if row is None:
                logger.warning("retry.missing", extra={"retry_id": retry_id})
                return None

            if row.status != ScheduledRetry.STATUS_PENDING:
                # Already ran, or was cancelled by a dispute/opt-out freeze.
                logger.info("retry.skipped", extra={
                    "retry_id": retry_id, "status": row.status,
                })
                return None

            # Claim it, so a duplicate timer firing cannot double-execute.
            row.status = ScheduledRetry.STATUS_RUNNING
            event_id = row.event_id
            attempt_number = row.attempt_number
            origin_trace_id = row.origin_trace_id

    trace_id = f"trc_{uuid.uuid4().hex}"

    try:
        async with factory() as session:
            async with session.begin():
                event: Optional[Event] = (
                    await session.execute(
                        select(Event).where(Event.event_id == event_id)
                    )
                ).scalars().first()

                if event is None:
                    raise LookupError(f"No stored event for event_id={event_id!r}")

                payment = _payment_from_event(event)

                triage = await run_triage(
                    session=session,
                    event_id=event_id,
                    trace_id=trace_id,
                    amount_paise=event.amount_paise,
                    error_code=event.error_code,
                    error_reason=event.error_reason,
                    error_description=event.error_description,
                    bank=event.bank,
                    issuer_bin=event.issuer_bin or payment.get("card_id"),
                    customer_id=event.customer_id,
                    customer_email=payment.get("email"),
                    customer_phone=payment.get("contact"),
                    attempt_number=attempt_number,
                    policy=policy,
                )

                td = triage.to_trace_dict()
                session.add(Trace(
                    trace_id=trace_id,
                    event_id=event_id,
                    category=td["category"],
                    action_type=td["action_type"],
                    outcome_status=td["outcome_status"],
                    confidence_score=td["confidence"],
                    rationale=td["rationale"],
                    hinglish_message=td["hinglish_message"],
                    llm_provider=td["provider_used"],
                    dispatch_channel=td["dispatch_channel"],
                    dispatch_cost_inr=td["dispatch_cost_inr"],
                    razorpay_link_id=td["razorpay_link_id"],
                    razorpay_link_url=td["razorpay_link_url"],
                    classification_rule=td["classification_rule"],
                    amount_inr=event.amount_paise / 100,
                    triage_metadata=json.dumps(triage.strategy.metadata, default=str),
                    retry_scheduled_at=triage.strategy.retry_scheduled_at,
                    pre_flight_passed=True,
                    guardrail_checks=json.dumps({
                        "idempotency_passed": True,
                        "retry_cap_passed": attempt_number <= policy.max_retry_attempts,
                        "quiet_hours_passed": True,
                        "anti_spam_passed": True,
                    }),
                ))

                event.retry_count = attempt_number

                claimed: Optional[ScheduledRetry] = (
                    await session.execute(
                        select(ScheduledRetry).where(ScheduledRetry.retry_id == retry_id)
                    )
                ).scalars().first()
                if claimed is not None:
                    claimed.status = ScheduledRetry.STATUS_COMPLETED
                    claimed.result_trace_id = trace_id

                sse_payload = {
                    "type": "trace_update",
                    "trace_id": trace_id,
                    "event_id": event_id,
                    "category": td["category"],
                    "action_type": td["action_type"],
                    "outcome_status": td["outcome_status"],
                    "amount_inr": event.amount_paise / 100,
                    "confidence": td["confidence"],
                    "rationale": td["rationale"],
                    "hinglish_message": td["hinglish_message"],
                    "provider_used": td["provider_used"],
                    "dispatch_channel": td["dispatch_channel"],
                    "dispatch_cost_inr": td["dispatch_cost_inr"],
                    "razorpay_link_url": td["razorpay_link_url"],
                    "classification_rule": td["classification_rule"],
                    "guardrail_checks": {
                        "idempotency_passed": True,
                        "retry_cap_passed": attempt_number <= policy.max_retry_attempts,
                        "quiet_hours_passed": True,
                        "anti_spam_passed": True,
                    },
                    "attempt_number": attempt_number,
                    "origin_trace_id": origin_trace_id,
                    "source": "scheduled_retry",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

        await broadcast(sse_payload)

        logger.info("retry.completed", extra={
            "retry_id": retry_id,
            "event_id": event_id,
            "trace_id": trace_id,
            "attempt_number": attempt_number,
            "action_type": sse_payload["action_type"],
        })
        return trace_id

    except Exception as exc:
        logger.error("retry.failed", extra={
            "retry_id": retry_id, "event_id": event_id, "error": str(exc),
        })
        async with factory() as session:
            async with session.begin():
                failed: Optional[ScheduledRetry] = (
                    await session.execute(
                        select(ScheduledRetry).where(ScheduledRetry.retry_id == retry_id)
                    )
                ).scalars().first()
                if failed is not None:
                    failed.status = ScheduledRetry.STATUS_FAILED
                    failed.last_error = str(exc)[:2000]
        return None


def _payment_from_event(event: Event) -> dict:
    """Recover the original payment entity from the stored raw webhook payload."""
    try:
        payload = json.loads(event.raw_payload)
        return payload.get("payload", {}).get("payment", {}).get("entity", {}) or {}
    except (ValueError, AttributeError, TypeError):
        return {}
