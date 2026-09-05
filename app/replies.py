"""
RevGuard — Inbound reply handling

Two entry points receive customer replies: the generic `POST /webhook/reply`
and Twilio's `POST /twilio/webhook` for real WhatsApp traffic.  Both must reach
the same conclusion about the same words, so the classification-and-freeze
decision lives here rather than being implemented once per transport.

That matters because the decision is a compliance one.  If a customer replies
"stop" over WhatsApp, the correct response is to freeze automation and cancel
every armed retry — not to generate a friendly assistant reply and carry on
dunning them.  Routing both transports through one function is what guarantees
the two cannot drift apart.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.classifier import classify
from app.db import Customer, CustomerReply, Event, Trace, get_session_factory
from app.logging_config import get_logger
from app.retry_queue import cancel_retries_for_event
from app.sse import broadcast

logger = get_logger(__name__)


@dataclass
class InboundReplyResult:
    """Outcome of processing one inbound reply."""

    reply_id: str
    category: str
    matched_rule: str
    is_stop_keyword: bool
    action_taken: str
    cancelled_retries: int
    event_id: Optional[str] = None

    @property
    def status(self) -> str:
        return "frozen" if self.is_stop_keyword else "acknowledged"


def normalise_phone(phone: Optional[str]) -> Optional[str]:
    """
    Reduce a phone number to comparable digits.

    Numbers reach us in several shapes — `whatsapp:+919000000000` from Twilio,
    `+91 90000 00000` from a webhook, a bare 10-digit number from a form — and
    a customer lookup that only matches one of them silently finds nobody.
    """
    if not phone:
        return None

    digits = "".join(ch for ch in phone.replace("whatsapp:", "") if ch.isdigit())
    if not digits:
        return None

    # Indian numbers: normalise to the 10 significant digits.
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


async def find_customer_by_phone(session, phone: Optional[str]) -> Optional[Customer]:
    """Resolve a customer from an inbound number, tolerating format differences."""
    target = normalise_phone(phone)
    if not target:
        return None

    rows = (
        await session.execute(
            select(Customer).where(Customer.phone.is_not(None))
        )
    ).scalars().all()

    for customer in rows:
        if normalise_phone(customer.phone) == target:
            return customer
    return None


async def find_context_for_phone(phone: Optional[str]) -> Optional[dict]:
    """
    Find the most recent failed payment belonging to *this* caller.

    Scoping by customer is not a nicety.  Answering with "the latest trace in
    the table" tells whoever texts in about somebody else's failed payment as
    soon as there is more than one customer in the system.
    """
    factory = get_session_factory()

    async with factory() as session:
        customer = await find_customer_by_phone(session, phone)
        if customer is None:
            return None

        event = (
            await session.execute(
                select(Event)
                .where(Event.customer_id == customer.customer_id)
                .order_by(Event.created_at.desc())
                .limit(1)
            )
        ).scalars().first()

        if event is None:
            return None

        trace = (
            await session.execute(
                select(Trace)
                .where(Trace.event_id == event.event_id)
                .order_by(Trace.created_at.desc())
                .limit(1)
            )
        ).scalars().first()

        return {
            "amount_inr": event.amount_paise / 100,
            "error_code": event.error_code or "unknown",
            "error_reason": event.error_reason or "unknown",
            "classification_rule": (trace.classification_rule if trace else None) or "unknown",
            "timestamp": str(event.created_at),
        }


async def record_inbound_reply(
    body: str,
    *,
    channel: str = "sms",
    phone: Optional[str] = None,
    customer_id: Optional[str] = None,
    event_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> InboundReplyResult:
    """
    Classify an inbound reply and, if it is a stop signal, freeze automation.

    On a stop keyword or dispute signal the response is immediate and total:
      1. the reply is stored verbatim, so the decision is auditable
      2. every pending retry for the payment is cancelled
      3. the customer is marked as contacted, freezing further outreach
      4. the freeze is broadcast to the dashboard

    Everything else is recorded and acknowledged without changing automation.
    """
    reply_id = f"rpl_{uuid.uuid4().hex[:16]}"
    now = now or datetime.now(timezone.utc)

    # The classifier is the single source of truth for what a reply means —
    # the same deterministic rules the payment path uses, no second opinion.
    classification = classify(
        error_code=None,
        error_reason=None,
        error_description=None,
        customer_reply=body,
    )
    is_stop = classification.is_stop_keyword

    logger.info("reply.received", extra={
        "reply_id": reply_id,
        "event_id": event_id,
        "channel": channel,
        "category": classification.category.value,
        "is_stop_keyword": is_stop,
    })

    cancelled = 0
    action_taken = "LOGGED_NO_ACTION"
    factory = get_session_factory()

    async with factory() as session:
        async with session.begin():
            # An inbound number may be all we have — resolve the customer from
            # it so a WhatsApp reply can freeze the right account.
            resolved_customer_id = customer_id
            if resolved_customer_id is None and phone:
                match = await find_customer_by_phone(session, phone)
                if match is not None:
                    resolved_customer_id = match.customer_id

            # Likewise the event: a reply rarely names the payment it concerns.
            resolved_event_id = event_id
            if is_stop and resolved_event_id is None and resolved_customer_id:
                latest = (
                    await session.execute(
                        select(Event)
                        .where(Event.customer_id == resolved_customer_id)
                        .order_by(Event.created_at.desc())
                        .limit(1)
                    )
                ).scalars().first()
                if latest is not None:
                    resolved_event_id = latest.event_id

            if is_stop:
                action_taken = "AUTOMATION_FROZEN"

                if resolved_event_id:
                    cancelled = await cancel_retries_for_event(
                        session,
                        resolved_event_id,
                        f"Customer reply classified as {classification.category.value}",
                    )

                # Push last_contacted_at forward so the anti-spam guardrail
                # suppresses outreach even on paths that do not consult the
                # retry queue at all.
                if resolved_customer_id:
                    customer = (
                        await session.execute(
                            select(Customer).where(
                                Customer.customer_id == resolved_customer_id
                            )
                        )
                    ).scalars().first()
                    if customer is None:
                        customer = Customer(
                            customer_id=resolved_customer_id, phone=phone
                        )
                        session.add(customer)
                    customer.last_contacted_at = now

            session.add(CustomerReply(
                reply_id=reply_id,
                event_id=resolved_event_id,
                customer_id=resolved_customer_id,
                phone=phone,
                channel=channel,
                body=body,
                category=classification.category.value,
                is_stop_keyword=is_stop,
                action_taken=action_taken,
                received_at=now,
            ))

    if is_stop:
        logger.warning("reply.automation_frozen", extra={
            "reply_id": reply_id,
            "event_id": resolved_event_id,
            "customer_id": resolved_customer_id,
            "cancelled_retries": cancelled,
            "matched_rule": classification.matched_rule,
        })
        await broadcast({
            "type": "automation_frozen",
            "reply_id": reply_id,
            "event_id": resolved_event_id,
            "customer_id": resolved_customer_id,
            "category": classification.category.value,
            "matched_rule": classification.matched_rule,
            "cancelled_retries": cancelled,
            "channel": channel,
            "body": body[:280],
            "timestamp": now.isoformat().replace("+00:00", "Z"),
        })

    return InboundReplyResult(
        reply_id=reply_id,
        category=classification.category.value,
        matched_rule=classification.matched_rule,
        is_stop_keyword=is_stop,
        action_taken=action_taken,
        cancelled_retries=cancelled,
        event_id=resolved_event_id,
    )
