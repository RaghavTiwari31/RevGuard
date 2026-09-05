"""
Tests for the Twilio WhatsApp transport and the shared inbound-reply logic.

The merge brought together two independently-built inbound paths: the generic
`POST /webhook/reply` and Twilio's `POST /twilio/webhook`. They must reach the
same conclusion about the same words — a "stop" over WhatsApp has to freeze
automation exactly as a "stop" over the generic endpoint does, or the
compliance guarantee only holds on one transport.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.db import CustomerReply, get_session_factory
from app.replies import find_context_for_phone, normalise_phone, record_inbound_reply


def _uid() -> str:
    return f"pay_{uuid.uuid4().hex[:14]}"


def _failure_payload(event_id: str, contact: str, customer_id: str,
                     error_code: str = "gateway_timeout") -> dict:
    return {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": event_id,
            "amount": 250000,
            "currency": "INR",
            "error_code": error_code,
            "error_description": f"Payment failed: {error_code}",
            "contact": contact,
            "email": "customer@example.com",
            "customer_id": customer_id,
            "bank": "HDFC Bank",
            "card_id": "411111000000",
        }}},
    }


@pytest.fixture(autouse=True)
def no_real_twilio():
    """Never let a test reach the Twilio API."""
    with patch("app.routers.twilio.send_whatsapp", return_value=True) as sender:
        yield sender


# ── Phone normalisation ───────────────────────────────────────────────────────

class TestPhoneNormalisation:
    def test_all_the_shapes_a_number_arrives_in_agree(self):
        """
        Twilio sends `whatsapp:+91...`, webhooks send `+91 ...`, forms send bare
        digits. A lookup that only matches one shape silently finds nobody.
        """
        forms = [
            "whatsapp:+919000000001",
            "+919000000001",
            "+91 90000 00001",
            "919000000001",
            "9000000001",
        ]
        assert len({normalise_phone(f) for f in forms}) == 1

    def test_junk_is_rejected_rather_than_guessed(self):
        assert normalise_phone(None) is None
        assert normalise_phone("") is None
        assert normalise_phone("not-a-number") is None


# ── The freeze, on both transports ────────────────────────────────────────────

class TestStopKeywordFreeze:
    async def test_twilio_stop_freezes_automation(self, test_client):
        """
        The bug this closes: Twilio's inbound path went straight to the LLM and
        never consulted the classifier, so "stop" over WhatsApp produced a
        cheerful assistant reply and left every retry armed.
        """
        event_id, phone, cust = _uid(), "+919000000042", "cust_twilio_stop"
        await test_client.post("/webhook", json=_failure_payload(event_id, phone, cust))
        assert (await test_client.get("/retries")).json()["pending"] >= 1

        from app.routers.twilio import process_and_reply
        await process_and_reply(f"whatsapp:{phone}", "STOP - this is unauthorized")

        still_armed = [
            r for r in (await test_client.get("/retries")).json()["retries"]
            if r["event_id"] == event_id and r["status"] == "pending"
        ]
        assert still_armed == [], "a disputed payment must have no armed retries"

    async def test_twilio_stop_does_not_call_the_llm(self, test_client):
        """A customer asking us to stop must not be answered by an improviser."""
        phone = "+919000000043"
        await test_client.post(
            "/webhook", json=_failure_payload(_uid(), phone, "cust_no_llm")
        )

        from app.routers.twilio import process_and_reply
        with patch("app.routers.twilio.generate_support_reply") as llm:
            await process_and_reply(f"whatsapp:{phone}", "please unsubscribe me")
            llm.assert_not_called()

    async def test_ordinary_message_still_gets_an_assistant_reply(self, test_client):
        phone = "+919000000044"
        await test_client.post(
            "/webhook", json=_failure_payload(_uid(), phone, "cust_ordinary")
        )

        from app.routers.twilio import process_and_reply
        with patch(
            "app.routers.twilio.generate_support_reply", return_value="Namaste!"
        ) as llm:
            await process_and_reply(f"whatsapp:{phone}", "when can I pay this?")
            llm.assert_called_once()

    async def test_ordinary_message_leaves_retries_armed(self, test_client):
        event_id, phone = _uid(), "+919000000045"
        await test_client.post(
            "/webhook", json=_failure_payload(event_id, phone, "cust_keep")
        )

        from app.routers.twilio import process_and_reply
        with patch("app.routers.twilio.generate_support_reply", return_value="ok"):
            await process_and_reply(f"whatsapp:{phone}", "sure, paying tomorrow")

        still_armed = [
            r for r in (await test_client.get("/retries")).json()["retries"]
            if r["event_id"] == event_id and r["status"] == "pending"
        ]
        assert len(still_armed) == 1

    async def test_both_transports_agree_on_the_same_words(self, test_client):
        """The point of sharing one implementation."""
        text = "STOP - I did not authorise this"

        generic = (await test_client.post(
            "/webhook/reply", json={"body": text, "customer_id": "cust_a"}
        )).json()

        twilio = await record_inbound_reply(text, channel="whatsapp", phone="+919000000046")

        assert generic["is_stop_keyword"] is twilio.is_stop_keyword is True
        assert generic["category"] == twilio.category
        assert generic["action_taken"] == twilio.action_taken == "AUTOMATION_FROZEN"


# ── Customer resolution and context scoping ───────────────────────────────────

class TestContextScoping:
    async def test_a_reply_resolves_its_own_customer_from_the_number(self, test_client):
        event_id, phone, cust = _uid(), "+919000000047", "cust_resolve"
        # The webhook records the customer and their number, which is what
        # lets an inbound reply resolve itself back to a payment.
        await test_client.post("/webhook", json=_failure_payload(event_id, phone, cust))

        result = await record_inbound_reply(
            "stop please", channel="whatsapp", phone=f"whatsapp:{phone}"
        )
        assert result.is_stop_keyword is True
        assert result.event_id == event_id, "freeze must target this caller's payment"

    async def test_context_is_scoped_to_the_caller(self, test_client):
        """
        Answering with the newest trace in the table describes one customer's
        payment to another as soon as there are two customers.
        """
        alice_event, alice_phone = _uid(), "+919000000048"
        bob_event, bob_phone = _uid(), "+919000000049"

        await test_client.post(
            "/webhook", json=_failure_payload(alice_event, alice_phone, "cust_alice",
                                              "insufficient_funds")
        )
        await test_client.post(
            "/webhook", json=_failure_payload(bob_event, bob_phone, "cust_bob",
                                              "gateway_timeout")
        )

        # Bob's failure is the most recent in the table. Alice must still hear
        # about her own payment, not his.
        context = await find_context_for_phone(f"whatsapp:{alice_phone}")
        assert context is not None
        assert context["error_code"] == "insufficient_funds"

    async def test_unknown_number_yields_no_context_rather_than_someone_elses(self, test_client):
        await test_client.post(
            "/webhook", json=_failure_payload(_uid(), "+919000000050", "cust_known")
        )
        assert await find_context_for_phone("+919999999999") is None


# ── Persistence ───────────────────────────────────────────────────────────────

class TestReplyPersistence:
    async def test_whatsapp_replies_are_stored_with_their_channel(self, test_client):
        await record_inbound_reply(
            "STOP", channel="whatsapp", phone="+919000000051", customer_id="cust_store"
        )

        factory = get_session_factory()
        async with factory() as session:
            stored = (
                await session.execute(
                    select(CustomerReply).where(CustomerReply.customer_id == "cust_store")
                )
            ).scalars().first()

        assert stored is not None
        assert stored.channel == "whatsapp"
        assert stored.is_stop_keyword is True
        assert stored.body == "STOP"


# ── The HTTP surface ──────────────────────────────────────────────────────────

class TestTwilioEndpoint:
    async def test_webhook_accepts_twilios_form_post(self, test_client):
        """Twilio posts form-encoded and needs a fast 200 or it retries."""
        response = await test_client.post(
            "/twilio/webhook",
            data={"From": "whatsapp:+919000000052", "Body": "hello"},
        )
        assert response.status_code == 200
        assert response.text == "OK"

    async def test_missing_fields_are_rejected(self, test_client):
        assert (await test_client.post("/twilio/webhook", data={})).status_code == 422
