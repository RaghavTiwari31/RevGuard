"""
RevGuard — Webhook router

Handles:
  POST /webhook        — Razorpay signed webhook (payment failures)
  POST /webhook/reply  — inbound customer SMS/WhatsApp reply

(/health lives in app.main — it is an app-level concern, not a webhook one.)

Signature verification is mandatory in production.  In development, an unset
RAZORPAY_WEBHOOK_SECRET degrades to a loud warning so the pipeline can be
exercised without Razorpay credentials; in production that same configuration
would leave an unauthenticated endpoint that triggers real outreach, so the
service refuses the request outright.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.classifier import classify
from app.db import Customer, CustomerReply, Event, Trace, get_session_factory
from app.guardrails import complete_idempotency, run_pre_flight
from app.logging_config import get_logger
from app.retry_queue import cancel_retries_for_event
from app.schemas import RazorpayWebhookPayload
from app.sse import broadcast
from app.triage import run_triage

logger = get_logger(__name__)
router = APIRouter()


# ── Signature verification ────────────────────────────────────────────────────

def is_production() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() == "production"


def require_signature(raw_body: bytes, signature: Optional[str]) -> None:
    """
    Enforce webhook authenticity, raising an HTTPException if it cannot be established.

    With a secret configured the HMAC must match.  Without one:
      - development → proceed, with a loud warning, so the pipeline is testable
        without Razorpay credentials
      - production  → refuse.  An unauthenticated webhook here means anyone who
        finds the URL can inject payment failures and make the system send real
        customer outreach.  Failing closed is the only safe default, and it
        surfaces the misconfiguration immediately instead of silently.
    """
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

    if not webhook_secret:
        if is_production():
            logger.error("webhook.rejected", extra={
                "reason": "RAZORPAY_WEBHOOK_SECRET is not set and APP_ENV=production",
            })
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Webhook signature verification is not configured. "
                    "Set RAZORPAY_WEBHOOK_SECRET before serving production traffic."
                ),
            )
        logger.warning("webhook.signature_verification_skipped", extra={
            "reason": "RAZORPAY_WEBHOOK_SECRET not set — skipping signature check (dev mode)",
        })
        return

    if not signature:
        logger.warning("webhook.rejected", extra={"reason": "Missing X-Razorpay-Signature header"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing webhook signature"
        )

    if not verify_razorpay_signature(raw_body, signature, webhook_secret):
        logger.warning("webhook.rejected", extra={"reason": "Invalid HMAC signature"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature"
        )



def verify_razorpay_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """
    Verify the Razorpay webhook HMAC-SHA256 signature.
    Razorpay signs the raw request body with the webhook secret.
    Header: X-Razorpay-Signature
    """
    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Webhook handler ───────────────────────────────────────────────────────────

@router.post("/webhook", tags=["webhook"])
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: Optional[str] = Header(default=None, alias="X-Razorpay-Signature"),
):
    """
    Main Razorpay webhook endpoint.

    Flow:
      1. Read raw body (needed for HMAC verification).
      2. Verify HMAC-SHA256 signature.
      3. Parse JSON payload.
      4. Extract event_id and payment metadata.
      5. Run Pre-Flight Invariant Engine (all four guardrail checks).
      6. Persist Event + Trace row.
      7. If pre-flight passed: run full triage pipeline.
      8. Update Trace row with triage result.
      9. Return 200 (Razorpay retries on non-2xx).
    """
    raw_body = await request.body()

    # ── 1. Signature verification ─────────────────────────────────────────────
    require_signature(raw_body, x_razorpay_signature)

    # ── 2. Parse payload ──────────────────────────────────────────────────────
    try:
        payload_dict = json.loads(raw_body)
        payload = RazorpayWebhookPayload(**payload_dict)
    except Exception as exc:
        logger.error("webhook.parse_error", extra={"error": str(exc)})
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid payload: {exc}")

    payment = payload.get_payment()
    event_id = payment.id if payment else payload_dict.get("id", f"unknown_{uuid.uuid4().hex[:8]}")
    customer_id = payment.customer_id if payment else None
    amount_paise = payment.amount if payment else 0
    trace_id = f"trc_{uuid.uuid4().hex}"

    logger.info("webhook.received", extra={
        "event_id": event_id,
        "event_type": payload.event,
        "trace_id": trace_id,
        "amount_paise": amount_paise,
    })

    # ── 3. Pre-Flight Invariant Engine ────────────────────────────────────────
    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            pre_flight = await run_pre_flight(
                session=session,
                event_id=event_id,
                trace_id=trace_id,
                customer_id=customer_id,
                now=datetime.now(timezone.utc),
            )

            # ── 4. Persist Event row (only on first idempotent pass) ──────────
            if pre_flight.idempotency.passed:
                event_row = Event(
                    event_id=event_id,
                    payment_id=payment.id if payment else None,
                    customer_id=customer_id,
                    amount_paise=amount_paise,
                    currency=payment.currency if payment else "INR",
                    error_code=payment.error_code if payment else None,
                    error_reason=payment.error_reason if payment else None,
                    error_description=payment.error_description if payment else None,
                    bank=payment.bank if payment else None,
                    raw_payload=raw_body.decode("utf-8", errors="replace"),
                )
                session.add(event_row)

            # ── 5. Persist initial Trace row ──────────────────────────────────
            trace_row = Trace(
                trace_id=trace_id,
                event_id=event_id,
                pre_flight_passed=pre_flight.passed,
                pre_flight_rejection_reason=pre_flight.rejection_reason,
                guardrail_checks=json.dumps(pre_flight.to_dict()),
                amount_inr=amount_paise / 100 if amount_paise else None,
            )
            session.add(trace_row)

    # ── 6. Pre-flight failed → return early ───────────────────────────────────
    if not pre_flight.passed:
        logger.info("webhook.pre_flight_rejected", extra={
            "event_id": event_id,
            "trace_id": trace_id,
            "reason": pre_flight.rejection_reason,
        })
        return {
            "status": "rejected",
            "trace_id": trace_id,
            "reason": pre_flight.rejection_reason,
        }

    logger.info("webhook.pre_flight_passed", extra={
        "event_id": event_id,
        "trace_id": trace_id,
    })

    # ── 7. Full Triage Pipeline ───────────────────────────────────────────────
    # Only runs for payment.failed events — skip for other event types
    if payload.event != "payment.failed" or payment is None:
        return {"status": "accepted", "trace_id": trace_id, "triage": "skipped"}

    async with factory() as session:
        async with session.begin():
            triage = await run_triage(
                session=session,
                event_id=event_id,
                trace_id=trace_id,
                amount_paise=amount_paise,
                error_code=payment.error_code,
                error_reason=payment.error_reason,
                error_description=payment.error_description,
                bank=payment.bank,
                issuer_bin=payment.card_id,   # card_id approximates BIN for now
                customer_id=customer_id,
                customer_name=None,
                customer_email=payment.email,
                customer_phone=payment.contact,
            )

            # ── 8. Update Trace row with triage result ────────────────────────
            triage_dict = triage.to_trace_dict()
            trace_row.category = triage_dict["category"]
            trace_row.action_type = triage_dict["action_type"]
            trace_row.outcome_status = triage_dict["outcome_status"]
            trace_row.confidence_score = triage_dict["confidence"]
            trace_row.rationale = triage_dict["rationale"]
            trace_row.hinglish_message = triage_dict["hinglish_message"]
            trace_row.llm_provider = triage_dict["provider_used"]
            trace_row.dispatch_channel = triage_dict["dispatch_channel"]
            trace_row.dispatch_cost_inr = triage_dict["dispatch_cost_inr"]
            trace_row.razorpay_link_id = triage_dict["razorpay_link_id"]
            trace_row.razorpay_link_url = triage_dict["razorpay_link_url"]
            trace_row.classification_rule = triage_dict["classification_rule"]
            trace_row.triage_metadata = json.dumps(triage.strategy.metadata)
            if triage.strategy.retry_scheduled_at:
                trace_row.retry_scheduled_at = triage.strategy.retry_scheduled_at

            session.add(trace_row)
            # The lock stops expiring now: this event is definitively processed,
            # so from here it is a genuine duplicate-suppression record.
            await complete_idempotency(session, event_id)

    logger.info("webhook.triage_complete", extra={
        "event_id": event_id,
        "trace_id": trace_id,
        "category": triage.category.value,
        "action_type": triage.action_type.value,
        "outcome_status": triage.outcome_status.value,
        "confidence": triage.confidence,
    })

    # Broadcast to SSE so the dashboard shows real webhook events live
    triage_dict = triage.to_trace_dict()
    await broadcast({
        "type": "trace_update",
        "event_id": event_id,
        "trace_id": trace_id,
        "index": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "category": triage_dict["category"],
        "action_type": triage_dict["action_type"],
        "outcome_status": triage_dict["outcome_status"],
        "confidence": triage_dict["confidence"],
        "rationale": triage_dict["rationale"],
        "hinglish_message": triage_dict["hinglish_message"],
        "provider_used": triage_dict["provider_used"],
        "dispatch_channel": triage_dict["dispatch_channel"],
        "amount_inr": amount_paise / 100 if amount_paise else 0,
        "razorpay_link_url": triage_dict["razorpay_link_url"],
        "classification_rule": triage_dict["classification_rule"],
        "guardrail_checks": pre_flight.to_dict(),
        "source": "live_webhook",
    })

    return {
        "status": "accepted",
        "trace_id": trace_id,
        "category": triage.category.value,
        "action_type": triage.action_type.value,
        "outcome_status": triage.outcome_status.value,
        "confidence": triage.confidence,
        "razorpay_link_url": triage.strategy.razorpay_payment_link_url,
    }


# ── Inbound customer replies ──────────────────────────────────────────────────

class CustomerReplyPayload(BaseModel):
    """
    An inbound SMS/WhatsApp reply.

    Shaped to be filled from any aggregator's inbound webhook — the field names
    are ours, the mapping is done by whatever forwards the message to us.
    """

    body: str = Field(..., min_length=1, max_length=4096, description="Raw message text")
    phone: Optional[str] = Field(default=None, max_length=32)
    customer_id: Optional[str] = Field(default=None, max_length=128)
    event_id: Optional[str] = Field(default=None, max_length=128, description="Payment this reply is about")
    channel: str = Field(default="sms", max_length=32)


@router.post("/webhook/reply", tags=["webhook"])
async def customer_reply(
    payload: CustomerReplyPayload,
    request: Request,
    x_razorpay_signature: Optional[str] = Header(default=None, alias="X-Razorpay-Signature"),
):
    """
    Handle an inbound customer reply.

    This closes the loop the classifier was already built for: `classify()` has
    always accepted a `customer_reply` argument, but nothing ever supplied one,
    so the DISPUTE_OR_OPTOUT path could only ever be reached through an error
    description — never through a customer actually saying "stop".

    On a stop keyword or dispute signal the response is immediate and total:
      1. the reply is stored verbatim, so the decision is auditable
      2. every pending retry for the payment is cancelled
      3. the customer is marked as contacted, freezing further outreach
      4. the freeze is broadcast to the dashboard

    Everything else is recorded and acknowledged without changing automation.
    """
    require_signature(await request.body(), x_razorpay_signature)

    reply_id = f"rpl_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)

    # The classifier is the single source of truth for what a reply means —
    # the same deterministic rules the payment path uses, no second opinion.
    classification = classify(
        error_code=None,
        error_reason=None,
        error_description=None,
        customer_reply=payload.body,
    )
    is_stop = classification.is_stop_keyword

    logger.info("reply.received", extra={
        "reply_id": reply_id,
        "event_id": payload.event_id,
        "channel": payload.channel,
        "category": classification.category.value,
        "is_stop_keyword": is_stop,
    })

    cancelled = 0
    action_taken = "LOGGED_NO_ACTION"
    factory = get_session_factory()

    async with factory() as session:
        async with session.begin():
            if is_stop:
                action_taken = "AUTOMATION_FROZEN"

                if payload.event_id:
                    cancelled = await cancel_retries_for_event(
                        session,
                        payload.event_id,
                        f"Customer reply classified as {classification.category.value}",
                    )

                # Push last_contacted_at forward so the anti-spam guardrail
                # suppresses outreach even on paths that do not consult the
                # retry queue at all.
                if payload.customer_id:
                    customer = (
                        await session.execute(
                            select(Customer).where(Customer.customer_id == payload.customer_id)
                        )
                    ).scalars().first()
                    if customer is None:
                        customer = Customer(
                            customer_id=payload.customer_id, phone=payload.phone
                        )
                        session.add(customer)
                    customer.last_contacted_at = now

            session.add(CustomerReply(
                reply_id=reply_id,
                event_id=payload.event_id,
                customer_id=payload.customer_id,
                phone=payload.phone,
                channel=payload.channel,
                body=payload.body,
                category=classification.category.value,
                is_stop_keyword=is_stop,
                action_taken=action_taken,
                received_at=now,
            ))

    if is_stop:
        logger.warning("reply.automation_frozen", extra={
            "reply_id": reply_id,
            "event_id": payload.event_id,
            "customer_id": payload.customer_id,
            "cancelled_retries": cancelled,
            "matched_rule": classification.matched_rule,
        })
        await broadcast({
            "type": "automation_frozen",
            "reply_id": reply_id,
            "event_id": payload.event_id,
            "customer_id": payload.customer_id,
            "category": classification.category.value,
            "matched_rule": classification.matched_rule,
            "cancelled_retries": cancelled,
            "body": payload.body[:280],
            "timestamp": now.isoformat().replace("+00:00", "Z"),
        })

    return {
        "status": "frozen" if is_stop else "acknowledged",
        "reply_id": reply_id,
        "category": classification.category.value,
        "matched_rule": classification.matched_rule,
        "is_stop_keyword": is_stop,
        "action_taken": action_taken,
        "cancelled_retries": cancelled,
    }
