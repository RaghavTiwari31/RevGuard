"""
Tests for the webhook endpoint — signature verification, CORS preflight,
and end-to-end pre-flight behaviour.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid


def _make_payload(event_id: str = None) -> dict:
    """Minimal Razorpay payment.failed webhook payload."""
    payment_id = event_id or f"pay_{uuid.uuid4().hex[:16]}"
    return {
        "entity": "event",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": 149900,
                    "currency": "INR",
                    "status": "failed",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Payment failed due to insufficient funds",
                    "error_reason": "payment_failed",
                    "error_source": "customer",
                    "error_step": "payment_authorization",
                }
            }
        },
        "created_at": 1725450000,
    }


class TestWebhookSignature:
    """Signature verification tests — no RAZORPAY_WEBHOOK_SECRET = dev mode."""

    async def test_webhook_accepted_in_dev_mode(self, test_client, monkeypatch):
        """Without RAZORPAY_WEBHOOK_SECRET, signature check is skipped."""
        monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)

        payload = _make_payload()
        response = await test_client.post(
            "/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200

    async def test_webhook_rejected_with_wrong_signature(self, test_client, monkeypatch):
        """When secret is set, wrong signature → 401."""
        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "test_secret")

        payload = _make_payload()
        response = await test_client.post(
            "/webhook",
            content=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": "bad_signature",
            },
        )
        assert response.status_code == 401

    async def test_webhook_accepted_with_correct_signature(self, test_client, monkeypatch):
        """Correct HMAC-SHA256 signature → accepted."""
        secret = "test_secret_key"
        monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", secret)

        payload = _make_payload()
        body = json.dumps(payload).encode()
        signature = hmac.new(
            key=secret.encode(),
            msg=body,
            digestmod=hashlib.sha256,
        ).hexdigest()

        response = await test_client.post(
            "/webhook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
            },
        )
        assert response.status_code == 200


class TestCORSPreflight:
    """
    DoD: A CORS OPTIONS preflight from localhost:5173 must return the correct
    Access-Control-Allow-Origin header.
    """

    async def test_cors_preflight_from_localhost(self, test_client):
        response = await test_client.options(
            "/webhook",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        # FastAPI CORSMiddleware returns 200 for OPTIONS with correct config
        assert response.status_code in (200, 204)
        origin = response.headers.get("access-control-allow-origin", "")
        assert origin == "http://localhost:5173", (
            f"Expected 'http://localhost:5173' in ACAO header, got: {origin!r}"
        )

    async def test_cors_preflight_blocked_for_unknown_origin(self, test_client):
        response = await test_client.options(
            "/webhook",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        origin = response.headers.get("access-control-allow-origin", "")
        assert "evil.example.com" not in origin


class TestWebhookIdempotency:
    """DoD: Same event_id posted twice → first accepted, second rejected."""

    async def test_duplicate_webhook_second_rejected(self, test_client, monkeypatch):
        monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)

        payload = _make_payload()
        body = json.dumps(payload)

        r1 = await test_client.post("/webhook", content=body,
                                    headers={"Content-Type": "application/json"})
        assert r1.status_code == 200
        assert r1.json()["status"] == "accepted"

        r2 = await test_client.post("/webhook", content=body,
                                    headers={"Content-Type": "application/json"})
        assert r2.status_code == 200
        # Second request must be rejected by idempotency lock
        assert r2.json()["status"] == "rejected"
        assert "IDEMPOTENCY" in r2.json()["reason"].upper()


class TestHealthEndpoint:
    async def test_health_returns_ok(self, test_client):
        response = await test_client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
