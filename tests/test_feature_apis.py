"""
Tests for the read/control APIs and the inbound reply path.

Covers:
  - GET /traces        — history that survives a page refresh
  - GET /issuers       — the Issuer Health Radar, previously computed but never exposed
  - GET /retries       — the retry queue
  - POST /webhook/reply — inbound customer replies freezing automation
  - production webhook signature enforcement
  - bandit weight persistence across a restart
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.bandit import (
    clear_persisted_state,
    flush_state,
    get_bandit_stats,
    load_state,
    record_reward,
    reset_bandit,
)
from app.channels import Channel
from app.db import CustomerReply, get_session_factory
from app.issuer_radar import IssuerBinStats


def _failure_payload(event_id: str, error_code: str = "insufficient_funds", amount: int = 250000) -> dict:
    return {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": event_id,
            "amount": amount,
            "currency": "INR",
            "error_code": error_code,
            "error_description": f"Payment failed: {error_code}",
            "contact": "+919000000000",
            "email": "customer@example.com",
            "customer_id": f"cust_{uuid.uuid4().hex[:8]}",
            "bank": "HDFC Bank",
            "card_id": "411111000000",
        }}},
    }


def _uid() -> str:
    return f"pay_{uuid.uuid4().hex[:14]}"


# ── Trace history (feature 2) ─────────────────────────────────────────────────

class TestTracesAPI:
    async def test_history_is_readable_after_the_stream_is_gone(self, test_client):
        """The whole point: a refresh must not lose everything."""
        event_id = _uid()
        await test_client.post("/webhook", json=_failure_payload(event_id))

        response = await test_client.get("/traces")
        assert response.status_code == 200

        body = response.json()
        assert body["total"] >= 1
        assert any(t["event_id"] == event_id for t in body["traces"])

    async def test_stored_traces_match_the_live_event_shape(self, test_client):
        """
        History rows and live SSE rows land in the same table in the dashboard,
        so they must carry the same keys.
        """
        await test_client.post("/webhook", json=_failure_payload(_uid()))
        trace = (await test_client.get("/traces")).json()["traces"][0]

        for key in (
            "type", "trace_id", "event_id", "category", "action_type",
            "outcome_status", "amount_inr", "confidence", "rationale",
            "hinglish_message", "dispatch_channel", "guardrail_checks", "timestamp",
        ):
            assert key in trace, f"missing {key}"
        assert trace["type"] == "trace_update"

    async def test_filters_and_pagination(self, test_client):
        for _ in range(3):
            await test_client.post("/webhook", json=_failure_payload(_uid()))

        page = (await test_client.get("/traces?limit=2")).json()
        assert len(page["traces"]) <= 2
        assert page["limit"] == 2

        filtered = (await test_client.get("/traces?category=TEMPORARY_CASHFLOW")).json()
        assert all(t["category"] == "TEMPORARY_CASHFLOW" for t in filtered["traces"])

        empty = (await test_client.get("/traces?category=NOT_A_CATEGORY")).json()
        assert empty["total"] == 0

    async def test_limit_is_capped(self, test_client):
        assert (await test_client.get("/traces?limit=9999")).status_code == 422

    async def test_single_trace_includes_every_attempt_for_its_event(self, test_client):
        event_id = _uid()
        await test_client.post("/webhook", json=_failure_payload(event_id))

        trace_id = (await test_client.get(f"/traces?event_id={event_id}")).json()["traces"][0]["trace_id"]
        detail = (await test_client.get(f"/traces/{trace_id}")).json()

        assert detail["trace"]["trace_id"] == trace_id
        assert len(detail["attempts"]) >= 1

    async def test_unknown_trace_is_404(self, test_client):
        assert (await test_client.get("/traces/trc_nope")).status_code == 404

    async def test_stats_rollup(self, test_client):
        await test_client.post("/webhook", json=_failure_payload(_uid()))
        stats = (await test_client.get("/traces/stats")).json()

        assert stats["total_traces"] >= 1
        assert stats["total_amount_inr"] > 0
        assert isinstance(stats["by_category"], list)
        assert isinstance(stats["by_action"], list)


# ── Issuer Health Radar (feature 5) ───────────────────────────────────────────

class TestIssuersAPI:
    async def test_empty_radar_is_not_an_error(self, test_client):
        body = (await test_client.get("/issuers")).json()
        assert body["issuers"] == []
        assert body["in_outage"] == 0

    async def test_failures_show_up_on_the_radar(self, test_client):
        await test_client.post("/webhook", json=_failure_payload(_uid()))

        body = (await test_client.get("/issuers")).json()
        assert body["total_tracked"] >= 1

        issuer = body["issuers"][0]
        assert issuer["bin"] == "411111"
        assert issuer["health"] in {"healthy", "degraded", "outage"}
        assert 0.0 <= issuer["pressure"] <= 1.0

    async def test_an_issuer_in_backoff_is_reported_as_an_outage(self, test_client):
        factory = get_session_factory()
        until = datetime.now(timezone.utc) + timedelta(hours=2)

        async with factory() as session:
            async with session.begin():
                session.add(IssuerBinStats(
                    bin="999888",
                    rolling_failures=0,
                    in_extended_backoff=1,
                    extended_backoff_until=until,
                    total_failures=45,
                ))

        body = (await test_client.get("/issuers")).json()
        outage = next(i for i in body["issuers"] if i["bin"] == "999888")

        assert outage["health"] == "outage"
        assert outage["in_extended_backoff"] is True
        assert outage["backoff_seconds_remaining"] > 0
        assert body["in_outage"] == 1

        only_bad = (await test_client.get("/issuers?only_unhealthy=true")).json()
        assert all(i["health"] != "healthy" for i in only_bad["issuers"])

    async def test_expired_backoff_is_no_longer_an_outage(self, test_client):
        factory = get_session_factory()
        async with factory() as session:
            async with session.begin():
                session.add(IssuerBinStats(
                    bin="777666",
                    in_extended_backoff=1,
                    extended_backoff_until=datetime.now(timezone.utc) - timedelta(hours=1),
                    total_failures=40,
                ))

        detail = (await test_client.get("/issuers/777666")).json()
        assert detail["in_extended_backoff"] is False

    async def test_unknown_bin_is_404(self, test_client):
        assert (await test_client.get("/issuers/000000")).status_code == 404


# ── Retry queue API (feature 1) ───────────────────────────────────────────────

class TestRetriesAPI:
    async def test_a_transient_failure_arms_a_retry(self, test_client):
        event_id = _uid()
        response = await test_client.post(
            "/webhook", json=_failure_payload(event_id, "gateway_timeout")
        )
        assert response.json()["action_type"] == "SCHEDULE_RETRY"

        queue = (await test_client.get("/retries")).json()
        assert queue["pending"] >= 1

        entry = next(r for r in queue["retries"] if r["event_id"] == event_id)
        assert entry["status"] == "pending"
        assert entry["attempt_number"] == 2
        assert entry["seconds_until_due"] > 0

    async def test_cancelling_a_retry(self, test_client):
        event_id = _uid()
        await test_client.post("/webhook", json=_failure_payload(event_id, "gateway_timeout"))

        retry_id = next(
            r["retry_id"]
            for r in (await test_client.get("/retries")).json()["retries"]
            if r["event_id"] == event_id
        )

        assert (await test_client.post(f"/retries/{retry_id}/cancel")).status_code == 200
        # Cancelling twice is a conflict, not a silent success.
        assert (await test_client.post(f"/retries/{retry_id}/cancel")).status_code == 409

    async def test_running_a_retry_on_demand(self, test_client):
        event_id = _uid()
        await test_client.post("/webhook", json=_failure_payload(event_id, "gateway_timeout"))

        retry_id = next(
            r["retry_id"]
            for r in (await test_client.get("/retries")).json()["retries"]
            if r["event_id"] == event_id
        )

        result = (await test_client.post(f"/retries/{retry_id}/run")).json()
        assert result["status"] == "completed"

        attempts = (await test_client.get(f"/traces?event_id={event_id}")).json()
        assert attempts["total"] == 2, "the replay should add a second attempt"

    async def test_unknown_retry_is_404(self, test_client):
        assert (await test_client.post("/retries/rty_nope/run")).status_code == 404


# ── Inbound replies (feature 4) ───────────────────────────────────────────────

class TestCustomerReplies:
    async def test_stop_keyword_freezes_automation_and_cancels_retries(self, test_client):
        event_id = _uid()
        await test_client.post("/webhook", json=_failure_payload(event_id, "gateway_timeout"))
        assert (await test_client.get("/retries")).json()["pending"] >= 1

        response = await test_client.post("/webhook/reply", json={
            "body": "STOP - this charge is unauthorized, I did not do this",
            "event_id": event_id,
            "customer_id": "cust_dispute",
            "phone": "+919000000000",
            "channel": "whatsapp",
        })

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "frozen"
        assert body["category"] == "DISPUTE_OR_OPTOUT"
        assert body["is_stop_keyword"] is True
        assert body["action_taken"] == "AUTOMATION_FROZEN"
        assert body["cancelled_retries"] >= 1

        pending = [
            r for r in (await test_client.get("/retries")).json()["retries"]
            if r["event_id"] == event_id and r["status"] == "pending"
        ]
        assert pending == [], "a disputed payment must have no armed retries"

    async def test_benign_reply_does_not_freeze(self, test_client):
        event_id = _uid()
        await test_client.post("/webhook", json=_failure_payload(event_id, "gateway_timeout"))

        body = (await test_client.post("/webhook/reply", json={
            "body": "sure, I will pay tomorrow morning",
            "event_id": event_id,
        })).json()

        assert body["status"] == "acknowledged"
        assert body["is_stop_keyword"] is False
        assert body["cancelled_retries"] == 0

        still_armed = [
            r for r in (await test_client.get("/retries")).json()["retries"]
            if r["event_id"] == event_id and r["status"] == "pending"
        ]
        assert len(still_armed) == 1

    async def test_reply_is_stored_verbatim_for_audit(self, test_client):
        text = "Please STOP contacting me about this"
        await test_client.post("/webhook/reply", json={"body": text, "customer_id": "cust_audit"})

        factory = get_session_factory()
        async with factory() as session:
            stored = (
                await session.execute(
                    select(CustomerReply).where(CustomerReply.customer_id == "cust_audit")
                )
            ).scalars().first()

        assert stored is not None
        assert stored.body == text
        assert stored.is_stop_keyword is True

    async def test_freeze_marks_the_customer_as_just_contacted(self, test_client):
        """
        Belt and braces: the anti-spam guardrail reads last_contacted_at, so a
        freeze must suppress outreach even on paths that never consult the
        retry queue.
        """
        from app.db import Customer

        await test_client.post("/webhook/reply", json={
            "body": "unsubscribe",
            "customer_id": "cust_cooldown",
        })

        factory = get_session_factory()
        async with factory() as session:
            customer = (
                await session.execute(
                    select(Customer).where(Customer.customer_id == "cust_cooldown")
                )
            ).scalars().first()

        assert customer is not None
        assert customer.last_contacted_at is not None

    async def test_empty_reply_is_rejected(self, test_client):
        assert (await test_client.post("/webhook/reply", json={"body": ""})).status_code == 422


# ── Production signature enforcement (feature 8) ──────────────────────────────

class TestSignatureEnforcement:
    @pytest.fixture(autouse=True)
    def clean_env(self):
        prev = {k: os.environ.get(k) for k in ("APP_ENV", "RAZORPAY_WEBHOOK_SECRET")}
        yield
        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    async def test_dev_without_a_secret_still_works(self, test_client):
        os.environ["APP_ENV"] = "development"
        os.environ.pop("RAZORPAY_WEBHOOK_SECRET", None)

        response = await test_client.post("/webhook", json=_failure_payload(_uid()))
        assert response.status_code == 200

    async def test_production_without_a_secret_fails_closed(self, test_client):
        """
        An unauthenticated webhook in production lets anyone who finds the URL
        inject failures and trigger real customer outreach. It must refuse.
        """
        os.environ["APP_ENV"] = "production"
        os.environ.pop("RAZORPAY_WEBHOOK_SECRET", None)

        response = await test_client.post("/webhook", json=_failure_payload(_uid()))
        assert response.status_code == 503
        assert "signature" in response.json()["detail"].lower()

    async def test_production_rejects_a_missing_signature_header(self, test_client):
        os.environ["APP_ENV"] = "production"
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test_secret"

        response = await test_client.post("/webhook", json=_failure_payload(_uid()))
        assert response.status_code == 401

    async def test_production_rejects_a_forged_signature(self, test_client):
        os.environ["APP_ENV"] = "production"
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test_secret"

        response = await test_client.post(
            "/webhook",
            json=_failure_payload(_uid()),
            headers={"X-Razorpay-Signature": "deadbeef"},
        )
        assert response.status_code == 401

    async def test_a_valid_signature_is_accepted(self, test_client):
        import hashlib
        import hmac

        os.environ["APP_ENV"] = "production"
        os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test_secret"

        payload = _failure_payload(_uid())
        raw = json.dumps(payload).encode()
        signature = hmac.new(b"test_secret", raw, hashlib.sha256).hexdigest()

        response = await test_client.post(
            "/webhook",
            content=raw,
            headers={
                "X-Razorpay-Signature": signature,
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200

    async def test_the_reply_endpoint_is_protected_too(self, test_client):
        os.environ["APP_ENV"] = "production"
        os.environ.pop("RAZORPAY_WEBHOOK_SECRET", None)

        response = await test_client.post("/webhook/reply", json={"body": "stop"})
        assert response.status_code == 503


# ── Bandit persistence (feature 3) ────────────────────────────────────────────

class TestBanditPersistence:
    async def test_weights_survive_a_restart(self, test_client):
        """
        The free-tier case: the process idles out. Without persistence the
        bandit relearns from zero every cold start and never learns at all.
        """
        await clear_persisted_state()

        for _ in range(5):
            record_reward("TEMPORARY_CASHFLOW", Channel.WHATSAPP, 0.9)
        for _ in range(3):
            record_reward("TEMPORARY_CASHFLOW", Channel.SMS, 0.4)

        before = get_bandit_stats()["TEMPORARY_CASHFLOW"]
        assert await flush_state() > 0

        # Simulate the restart: memory is gone, the table is not.
        reset_bandit()
        assert get_bandit_stats() == {}

        await load_state()
        after = get_bandit_stats()["TEMPORARY_CASHFLOW"]

        assert after["whatsapp"]["pulls"] == before["whatsapp"]["pulls"]
        assert after["whatsapp"]["mean_reward"] == before["whatsapp"]["mean_reward"]
        assert after["sms"]["pulls"] == before["sms"]["pulls"]

    async def test_loading_an_empty_table_is_fine(self, test_client):
        await clear_persisted_state()
        result = await load_state()
        assert result["loaded"] is True
        assert result["arms"] == 0

    async def test_clearing_wipes_both_memory_and_table(self, test_client):
        record_reward("SEG_X", Channel.SMS, 1.0)
        await flush_state()

        await clear_persisted_state()
        assert get_bandit_stats() == {}

        await load_state()
        assert get_bandit_stats() == {}
