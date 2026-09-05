"""
RevGuard — Pydantic schemas

Covers:
  - Razorpay webhook payload (inbound)
  - Trace / SSE event (outbound)
  - Health check response
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, ValidationError, field_serializer

# ── Razorpay Webhook Schemas ──────────────────────────────────────────────────

class RazorpayPaymentEntity(BaseModel):
    """Subset of fields from a Razorpay payment entity we care about."""

    id: str                                         # pay_XXXXXXXXXXXXXXXX
    amount: int                                     # in paise
    currency: str = "INR"
    status: Optional[str] = None
    order_id: Optional[str] = None
    description: Optional[str] = None
    email: Optional[str] = None
    contact: Optional[str] = None

    # Error metadata (present on failed payments)
    error_code: Optional[str] = None
    error_description: Optional[str] = None
    error_reason: Optional[str] = None
    error_source: Optional[str] = None
    error_step: Optional[str] = None

    # Card / bank metadata
    bank: Optional[str] = None
    card_id: Optional[str] = None

    # Customer
    customer_id: Optional[str] = None

    model_config = {"extra": "allow"}   # Razorpay adds fields; don't error


class RazorpayWebhookPayload(BaseModel):
    """
    Top-level Razorpay webhook envelope.
    Ref: https://razorpay.com/docs/webhooks/payloads/payments/
    """

    entity: str = "event"
    account_id: Optional[str] = None
    event: str                                      # e.g. "payment.failed"
    contains: list[str] = Field(default_factory=list)
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[int] = None                # Unix timestamp

    model_config = {"extra": "allow"}

    def get_payment(self) -> Optional[RazorpayPaymentEntity]:
        """Extract the payment entity from the nested payload, if present."""
        try:
            return RazorpayPaymentEntity(**self.payload["payment"]["entity"])
        except (KeyError, TypeError, ValidationError):
            return None


# ── SSE / Trace Event Schema ──────────────────────────────────────────────────

class GuardrailChecks(BaseModel):
    idempotency_passed: bool
    retry_cap_passed: bool
    quiet_hours_passed: bool
    anti_spam_passed: bool


class TraceEvent(BaseModel):
    """
    Fixed SSE event contract (§3 of the implementation plan).
    Both the FastAPI SSE emitter and the React dashboard parse this shape.
    """

    type: str = "trace_update"
    trace_id: str                                   # trc_<uuid4>
    event_id: str                                   # pay_... or sim_...
    category: Optional[str] = None                  # TEMPORARY_CASHFLOW, etc.
    action_type: Optional[str] = None              # GENERATE_PAYMENT_LINK, etc.
    outcome_status: Optional[str] = None
    amount_inr: Optional[float] = None
    confidence: Optional[float] = None
    guardrail_checks: Optional[GuardrailChecks] = None
    pre_flight_rejection_reason: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_serializer("timestamp")
    def serialize_timestamp(self, v: datetime) -> str:
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ── Health Check ──────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    db: str = "connected"
    env: str = "development"
    policy_loaded: bool = True
