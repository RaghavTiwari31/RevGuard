"""
Regression tests for the live policy editor and the health endpoint.

Pins down two bugs that were live:
  - POST /policy/update wrote straight onto the singleton without re-validating,
    so a malformed quiet-hours value was accepted and then crashed the guardrail
    that read it
  - /health was registered twice (once in app.main, once in the webhook router)
"""

from __future__ import annotations

import pytest

import app.policy as policy_module
from app.guardrails import check_quiet_hours
from app.main import app
from app.policy import Policy, load_policy


@pytest.fixture(autouse=True)
def restore_policy():
    """Every test here mutates the singleton — put it back afterwards."""
    original = policy_module._policy
    original_mtime = policy_module._policy_mtime
    yield
    policy_module._policy = original
    policy_module._policy_mtime = original_mtime


# ── Routing ───────────────────────────────────────────────────────────────────

def test_health_is_registered_exactly_once():
    """
    Two routes on the same path meant only the first was reachable and the
    other was silently dead, while /docs advertised both.
    """
    health_routes = [r for r in app.routes if getattr(r, "path", None) == "/health"]
    assert len(health_routes) == 1


class TestHealthPayload:
    async def test_reports_db_and_env(self, test_client):
        response = await test_client.get("/health")
        assert response.status_code == 200

        body = response.json()
        assert body["status"] == "ok"
        assert body["db"] == "connected"
        assert body["policy_loaded"] is True
        assert "env" in body


# ── Policy model validation ───────────────────────────────────────────────────

class TestPolicyAssignmentValidation:
    def test_malformed_quiet_hours_is_rejected_at_assignment(self):
        policy = Policy()
        with pytest.raises(Exception):
            policy.quiet_hours_start = "not-a-time"

    def test_out_of_range_confidence_is_rejected_at_assignment(self):
        policy = Policy()
        with pytest.raises(Exception):
            policy.min_confidence_for_autonomous_action = 1.5

    def test_a_valid_assignment_still_works(self):
        policy = Policy()
        policy.quiet_hours_start = "22:30"
        assert policy.quiet_hours_start == "22:30"


# ── Live policy editor ────────────────────────────────────────────────────────

class TestPolicyUpdateEndpoint:
    async def test_valid_patch_is_applied(self, test_client):
        response = await test_client.post(
            "/policy/update", json={"min_confidence_for_autonomous_action": 0.9}
        )
        assert response.status_code == 200
        assert response.json()["policy"]["min_confidence_for_autonomous_action"] == 0.9

    async def test_malformed_quiet_hours_is_rejected_with_422(self, test_client):
        response = await test_client.post(
            "/policy/update", json={"quiet_hours_start": "25:99"}
        )
        assert response.status_code == 422

    async def test_a_rejected_patch_leaves_the_live_policy_untouched(self, test_client):
        """
        A patch is all-or-nothing.  Half-applying a guardrail config is worse
        than refusing it: the fields are read independently by different checks.
        """
        before = (await test_client.get("/policy")).json()

        response = await test_client.post(
            "/policy/update",
            json={"anti_spam_cooldown_hours": 8, "quiet_hours_start": "nonsense"},
        )
        assert response.status_code == 422

        after = (await test_client.get("/policy")).json()
        assert after == before

        # And the guardrail that reads it still works.
        check_quiet_hours(None, policy_module.get_policy())

    async def test_empty_patch_is_rejected(self, test_client):
        assert (await test_client.post("/policy/update", json={})).status_code == 422

    async def test_reset_restores_the_file_values(self, test_client):
        await test_client.post(
            "/policy/update", json={"min_confidence_for_autonomous_action": 0.99}
        )
        response = await test_client.post("/policy/reset")
        assert response.status_code == 200

        from_file = load_policy("policy.yaml")
        assert (
            response.json()["policy"]["min_confidence_for_autonomous_action"]
            == from_file.min_confidence_for_autonomous_action
        )

    async def test_bandit_toggle_is_exposed_and_patchable(self, test_client):
        assert "enable_adaptive_channel_bandit" in (await test_client.get("/policy")).json()

        response = await test_client.post(
            "/policy/update", json={"enable_adaptive_channel_bandit": False}
        )
        assert response.status_code == 200
        assert response.json()["policy"]["enable_adaptive_channel_bandit"] is False
