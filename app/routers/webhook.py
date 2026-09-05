"""
RevGuard — Webhook router

Handles:
  POST /webhook  — Razorpay signed webhook
  GET  /health   — Health check (also used by Render pinger)

Phase 2 update: wires in the full triage pipeline after pre-flight.
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
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Customer, Event, Trace, get_session_factory
from app.guardrails import run_pre_flight
from app.logging_config import get_logger
from app.policy import get_policy
from app.schemas import HealthResponse, RazorpayWebhookPayload, TraceEvent
from app.sse import broadcast
from app.triage import run_triage

logger = get_logger(__name__)
router = APIRouter()


# ── Signature verification ────────────────────────────────────────────────────

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


# ── Health check ──────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse, tags=["ops"])
async def health_check():
    """Lightweight health check — used by Render pinger and judges."""
    policy = get_policy()
    return HealthResponse(
        status="ok",
        version="1.0.0",
        db="connected",
        policy_loaded=policy is not None,
    )


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
    webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

    if webhook_secret:
        if not x_razorpay_signature:
            logger.warning("webhook.rejected", extra={"reason": "Missing X-Razorpay-Signature header"})
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing webhook signature")

        if not verify_razorpay_signature(raw_body, x_razorpay_signature, webhook_secret):
            logger.warning("webhook.rejected", extra={"reason": "Invalid HMAC signature"})
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")
    else:
        logger.warning("webhook.signature_verification_skipped", extra={
            "reason": "RAZORPAY_WEBHOOK_SECRET not set — skipping signature check (dev mode)"
        })

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
