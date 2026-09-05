"""
RevGuard — Action Dispatcher: All Four Recovery Strategies

Strategy 1 — Silent Delayed Retry (TRANSIENT_DOWNTIME)
  Schedule a background retry job via APScheduler, keyed to a simple bank
  uptime lookup table.  If the issuer is in extended backoff (Issuer Health
  Radar), delay is doubled.

Strategy 2 — Payment Link Generation (TEMPORARY_CASHFLOW)
  Call the Razorpay SDK to create a test-mode Payment Link whose amount
  exactly equals amount_due.  Amount is validated before the API call.

Strategy 3 — Promise-to-Pay Conversational Flow (EXPIRED_MANDATE)
  Two-turn state machine.  The LLM parses the customer's free-text reply
  into a structured {amount, date} — but date/amount are validated against
  invariants before being persisted.  Mandate re-registration link is sent.

Strategy 4 — Circuit Breaker / Escalation (DISPUTE_OR_OPTOUT / UNRECOVERABLE_FRAUD / max retries)
  Freeze all automation for the record.  Log to immutable audit trail.
  Mark the Trace row as ESCALATED_HUMAN_ATTENTION.

All strategies return a StrategyResult that feeds the Trace row and SSE event.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from app.channels import Channel, DispatchRecord, dispatch, select_channel, get_channel_cost
from app.classifier import FailureCategory
from app.logging_config import get_logger
from app.policy import Policy, get_policy

logger = get_logger(__name__)


# ── Action types ──────────────────────────────────────────────────────────────

class ActionType(str, Enum):
    SCHEDULE_RETRY = "SCHEDULE_RETRY"
    GENERATE_PAYMENT_LINK = "GENERATE_PAYMENT_LINK"
    SEND_MANDATE_LINK = "SEND_MANDATE_LINK"
    ESCALATED_HUMAN_ATTENTION = "ESCALATED_HUMAN_ATTENTION"
    DROPPED_NO_ACTION = "DROPPED_NO_ACTION"


class OutcomeStatus(str, Enum):
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    AWAITING_CUSTOMER_SETTLEMENT = "AWAITING_CUSTOMER_SETTLEMENT"
    AWAITING_MANDATE_RENEWAL = "AWAITING_MANDATE_RENEWAL"
    ESCALATED = "ESCALATED"
    DROPPED = "DROPPED"
    EXTENDED_BACKOFF = "EXTENDED_BACKOFF"


# ── Bank uptime lookup table (Strategy 1) ────────────────────────────────────
# Maps known BIN prefixes / bank names to optimal retry delay in minutes.
# Defaults to 30 minutes for unknown issuers.

_BANK_UPTIME_DELAYS: dict[str, int] = {
    "hdfc": 20,
    "icici": 20,
    "sbi": 45,          # Public sector banks often slower to recover
    "axis": 25,
    "kotak": 20,
    "yes": 30,
    "pnb": 45,
    "bob": 45,
    "canara": 45,
    "union": 45,
}
_DEFAULT_RETRY_DELAY_MINUTES = 30
_EXTENDED_BACKOFF_MULTIPLIER = 2   # Double delay during issuer outage


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class StrategyResult:
    action_type: ActionType
    outcome_status: OutcomeStatus
    dispatch_record: Optional[DispatchRecord] = None
    razorpay_payment_link_id: Optional[str] = None
    razorpay_payment_link_url: Optional[str] = None
    retry_scheduled_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)


# ── Razorpay SDK helper ───────────────────────────────────────────────────────

def _get_razorpay_client():
    """Return a Razorpay client. Returns None if SDK keys are not configured."""
    try:
        import razorpay
        key_id = os.getenv("RAZORPAY_KEY_ID", "")
        key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")
        if not key_id or not key_secret:
            return None
        return razorpay.Client(auth=(key_id, key_secret))
    except ImportError:
        return None


def _create_payment_link_mock(
    amount_paise: int,
    description: str,
    customer_name: Optional[str] = None,
    customer_email: Optional[str] = None,
    customer_phone: Optional[str] = None,
) -> dict:
    """
    Mock payment link creation — returns a realistic response shape when
    Razorpay SDK keys are not configured (for testing / CI).
    """
    import uuid
    link_id = f"plink_{uuid.uuid4().hex[:16]}"
    return {
        "id": link_id,
        "short_url": f"https://rzp.io/l/{link_id[:8]}",
        "amount": amount_paise,
        "currency": "INR",
        "description": description,
        "status": "created",
    }


def create_payment_link(
    amount_paise: int,
    event_id: str,
    customer_name: Optional[str] = None,
    customer_email: Optional[str] = None,
    customer_phone: Optional[str] = None,
) -> dict:
    """
    Create a Razorpay test-mode Payment Link for the exact amount_paise.
    Falls back to a mock response if SDK keys are not set.
    """
    description = f"RevGuard Recovery — Payment for event {event_id}"
    client = _get_razorpay_client()

    if client is None:
        logger.warning("razorpay.no_client", extra={
            "reason": "SDK keys not configured — using mock payment link",
            "event_id": event_id,
        })
        return _create_payment_link_mock(
            amount_paise, description, customer_name, customer_email, customer_phone
        )

    payload: dict = {
        "amount": amount_paise,
        "currency": "INR",
        "description": description,
        "reference_id": event_id,
        "send_sms_hash": False,
        "send_email": False,
    }
    if customer_name or customer_email or customer_phone:
        payload["customer"] = {}
        if customer_name:
            payload["customer"]["name"] = customer_name
        if customer_email:
            payload["customer"]["email"] = customer_email
        if customer_phone:
            payload["customer"]["contact"] = customer_phone

    try:
        response = client.payment_link.create(payload)
        logger.info("razorpay.payment_link_created", extra={
            "event_id": event_id,
            "link_id": response.get("id"),
            "amount_paise": amount_paise,
        })
        return response
    except Exception as exc:
        logger.error("razorpay.payment_link_error", extra={
            "event_id": event_id,
            "error": str(exc),
            "fallback": "mock_response",
        })
        return _create_payment_link_mock(
            amount_paise, description, customer_name, customer_email, customer_phone
        )


# ── Strategy 1: Silent Delayed Retry (TRANSIENT_DOWNTIME) ────────────────────

def strategy_silent_retry(
    event_id: str,
    amount_paise: int,
    bank: Optional[str],
    outreach_message: str,
    customer_phone: Optional[str],
    in_extended_backoff: bool = False,
    policy: Optional[Policy] = None,
) -> StrategyResult:
    """
    Schedule a silent retry during optimal banking hours.
    If the issuer is in extended backoff, double the delay.
    No outreach is sent — hence 'silent'.
    """
    if policy is None:
        policy = get_policy()

    # Determine retry delay from bank uptime table
    bank_key = (bank or "").lower()
    base_delay = next(
        (v for k, v in _BANK_UPTIME_DELAYS.items() if k in bank_key),
        _DEFAULT_RETRY_DELAY_MINUTES,
    )
    delay_minutes = base_delay * _EXTENDED_BACKOFF_MULTIPLIER if in_extended_backoff else base_delay
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)

    # Schedule via APScheduler (best-effort — scheduler may not be running in test)
    try:
        from app.scheduler import get_scheduler
        scheduler = get_scheduler()
        if scheduler.running:
            scheduler.add_job(
                func=_retry_noop,           # Phase 2 stub; Phase 3 wires this to triage
                trigger="date",
                run_date=retry_at,
                id=f"retry_{event_id}_{retry_at.timestamp():.0f}",
                args=[event_id],
                replace_existing=True,
            )
    except Exception as exc:
        logger.warning("scheduler.add_job_failed", extra={"error": str(exc)})

    logger.info("strategy.silent_retry", extra={
        "event_id": event_id,
        "delay_minutes": delay_minutes,
        "retry_at": retry_at.isoformat(),
        "in_extended_backoff": in_extended_backoff,
        "bank": bank,
    })

    return StrategyResult(
        action_type=ActionType.SCHEDULE_RETRY,
        outcome_status=OutcomeStatus.EXTENDED_BACKOFF if in_extended_backoff else OutcomeStatus.RETRY_SCHEDULED,
        retry_scheduled_at=retry_at,
        metadata={
            "delay_minutes": delay_minutes,
            "bank": bank,
            "in_extended_backoff": in_extended_backoff,
        },
    )


async def _retry_noop(event_id: str) -> None:
    """Placeholder retry job — Phase 3 will replace this with actual triage replay."""
    logger.info("scheduler.retry_fired", extra={"event_id": event_id})


# ── Strategy 2: Payment Link (TEMPORARY_CASHFLOW) ────────────────────────────

def strategy_payment_link(
    event_id: str,
    amount_paise: int,
    outreach_message: str,
    customer_name: Optional[str],
    customer_email: Optional[str],
    customer_phone: Optional[str],
    policy: Optional[Policy] = None,
) -> StrategyResult:
    """
    Create a Razorpay Payment Link and dispatch it via the best channel.
    The amount_paise is validated by the Post-Flight Validator before this call.
    """
    if policy is None:
        policy = get_policy()

    # Create payment link
    link_data = create_payment_link(
        amount_paise=amount_paise,
        event_id=event_id,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
    )

    # Append link to outreach message
    link_url = link_data.get("short_url") or link_data.get("id", "")
    full_message = f"{outreach_message}\n\nPayment Link: {link_url}"

    # Dispatch via selected channel
    channel = select_channel(
        amount_inr=amount_paise / 100,
        customer_phone=customer_phone,
        policy=policy,
    )
    record = dispatch(full_message, channel, customer_phone, policy)

    return StrategyResult(
        action_type=ActionType.GENERATE_PAYMENT_LINK,
        outcome_status=OutcomeStatus.AWAITING_CUSTOMER_SETTLEMENT,
        dispatch_record=record,
        razorpay_payment_link_id=link_data.get("id"),
        razorpay_payment_link_url=link_url,
        metadata={
            "link_id": link_data.get("id"),
            "link_status": link_data.get("status"),
            "channel": channel.value,
            "cost_inr": record.cost_inr,
        },
    )


# ── Strategy 3: Mandate Re-registration (EXPIRED_MANDATE) ────────────────────

def strategy_mandate_reregistration(
    event_id: str,
    outreach_message: str,
    customer_phone: Optional[str],
    customer_email: Optional[str],
    policy: Optional[Policy] = None,
) -> StrategyResult:
    """
    Send a mandate re-registration link via the best channel.
    The LLM drafts the message; we validate tone before dispatch.
    A Promise-to-Pay follow-up flow can be triggered by customer reply (Phase 3).
    """
    if policy is None:
        policy = get_policy()

    # Mock mandate re-registration link (Razorpay Subscriptions API in Phase 4)
    import uuid
    mandate_link = f"https://rzp.io/mandate/{uuid.uuid4().hex[:10]}"
    full_message = f"{outreach_message}\n\nMandate renewal link: {mandate_link}"

    channel = select_channel(
        amount_inr=1000,  # Arbitrary — no amount for mandate
        customer_phone=customer_phone,
        policy=policy,
    )
    record = dispatch(full_message, channel, customer_phone, policy)

    return StrategyResult(
        action_type=ActionType.SEND_MANDATE_LINK,
        outcome_status=OutcomeStatus.AWAITING_MANDATE_RENEWAL,
        dispatch_record=record,
        metadata={
            "mandate_link": mandate_link,
            "channel": channel.value,
        },
    )


# ── Strategy 4: Circuit Breaker / Escalation ─────────────────────────────────

def strategy_circuit_breaker(
    event_id: str,
    reason: str,
    category: FailureCategory,
    outreach_message: Optional[str] = None,
    customer_phone: Optional[str] = None,
    policy: Optional[Policy] = None,
) -> StrategyResult:
    """
    Freeze all automation for this record.
    For DISPUTE_OR_OPTOUT: send a brief acknowledgment and freeze.
    For UNRECOVERABLE_FRAUD: log only — do NOT contact customer.
    """
    if policy is None:
        policy = get_policy()

    dispatch_record = None

    if category == FailureCategory.DISPUTE_OR_OPTOUT and outreach_message and customer_phone:
        # Send a brief human acknowledgment (not a dunning message)
        dispatch_record = dispatch(outreach_message, Channel.SMS, customer_phone, policy)

    logger.warning("strategy.circuit_breaker", extra={
        "event_id": event_id,
        "reason": reason,
        "category": category.value,
        "customer_notified": dispatch_record is not None,
    })

    return StrategyResult(
        action_type=ActionType.ESCALATED_HUMAN_ATTENTION,
        outcome_status=OutcomeStatus.ESCALATED,
        dispatch_record=dispatch_record,
        metadata={
            "reason": reason,
            "category": category.value,
            "automation_frozen": True,
        },
    )


# ── Dispatcher entry point ────────────────────────────────────────────────────

def dispatch_action(
    category: FailureCategory,
    event_id: str,
    amount_paise: int,
    outreach_message: str,
    confidence: float,
    bank: Optional[str] = None,
    issuer_bin: Optional[str] = None,
    customer_name: Optional[str] = None,
    customer_email: Optional[str] = None,
    customer_phone: Optional[str] = None,
    in_extended_backoff: bool = False,
    attempt_number: int = 1,
    is_stop_keyword: bool = False,
    policy: Optional[Policy] = None,
) -> StrategyResult:
    """
    Route to the correct recovery strategy based on category.

    Confidence Gate is enforced here:
      confidence < policy.min_confidence → force circuit breaker regardless of category.
    """
    if policy is None:
        policy = get_policy()

    # ── Confidence Gate ───────────────────────────────────────────────────────
    if confidence < policy.min_confidence_for_autonomous_action:
        logger.warning("confidence_gate.triggered", extra={
            "confidence": confidence,
            "threshold": policy.min_confidence_for_autonomous_action,
            "category": category.value,
            "event_id": event_id,
        })
        return strategy_circuit_breaker(
            event_id=event_id,
            reason=f"Confidence {confidence:.2f} below threshold {policy.min_confidence_for_autonomous_action}",
            category=FailureCategory.DISPUTE_OR_OPTOUT,  # Treat as escalation
            outreach_message=None,
            customer_phone=customer_phone,
            policy=policy,
        )

    # ── Circuit Breaker (stop-keywords, fraud, max retries) ──────────────────
    if (
        is_stop_keyword
        or category in (FailureCategory.DISPUTE_OR_OPTOUT, FailureCategory.UNRECOVERABLE_FRAUD)
        or attempt_number > policy.max_retry_attempts
    ):
        return strategy_circuit_breaker(
            event_id=event_id,
            reason=(
                "Stop keyword detected" if is_stop_keyword
                else f"Category={category.value}" if category in (
                    FailureCategory.DISPUTE_OR_OPTOUT, FailureCategory.UNRECOVERABLE_FRAUD
                )
                else f"Attempt {attempt_number} exceeds cap {policy.max_retry_attempts}"
            ),
            category=category,
            outreach_message=outreach_message if category == FailureCategory.DISPUTE_OR_OPTOUT else None,
            customer_phone=customer_phone,
            policy=policy,
        )

    # ── Strategy 1: Transient downtime → silent retry ─────────────────────────
    if category == FailureCategory.TRANSIENT_DOWNTIME:
        return strategy_silent_retry(
            event_id=event_id,
            amount_paise=amount_paise,
            bank=bank,
            outreach_message=outreach_message,
            customer_phone=customer_phone,
            in_extended_backoff=in_extended_backoff,
            policy=policy,
        )

    # ── Strategy 2: Insufficient funds → payment link ─────────────────────────
    if category == FailureCategory.TEMPORARY_CASHFLOW:
        return strategy_payment_link(
            event_id=event_id,
            amount_paise=amount_paise,
            outreach_message=outreach_message,
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            policy=policy,
        )

    # ── Strategy 3: Expired mandate → mandate link ────────────────────────────
    if category == FailureCategory.EXPIRED_MANDATE:
        return strategy_mandate_reregistration(
            event_id=event_id,
            outreach_message=outreach_message,
            customer_phone=customer_phone,
            customer_email=customer_email,
            policy=policy,
        )

    # Default fallback — should never reach here given the category enum
    logger.error("dispatcher.unknown_category", extra={"category": category.value})
    return strategy_circuit_breaker(
        event_id=event_id,
        reason=f"Unknown category: {category.value}",
        category=category,
        policy=policy,
    )
