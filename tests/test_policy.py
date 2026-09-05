"""
Unit tests for the policy loader (app/policy.py).

Verifies that:
  - policy.yaml is correctly parsed and validated
  - Field types and constraints are enforced by Pydantic
  - Defaults are used when the file is absent
  - Mutating values in the YAML changes what the loader returns
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from app.policy import Policy, load_policy


class TestPolicyLoader:

    def test_loads_from_file(self, tmp_path: Path):
        yaml_content = textwrap.dedent("""\
            max_retry_attempts: 5
            anti_spam_cooldown_hours: 6
            quiet_hours_start: "22:00"
            quiet_hours_end: "08:00"
            timezone: "Asia/Kolkata"
            min_confidence_for_autonomous_action: 0.80
            voice_call_min_amount_inr: 200
            channel_unit_cost_inr:
              sms: 0.25
              whatsapp: 0.50
              voice: 3.00
            stop_keywords:
              - "unauthorized"
              - "fraud"
        """)
        p = tmp_path / "policy.yaml"
        p.write_text(yaml_content)

        policy = load_policy(p)
        assert policy.max_retry_attempts == 5
        assert policy.anti_spam_cooldown_hours == 6
        assert policy.quiet_hours_start == "22:00"
        assert policy.min_confidence_for_autonomous_action == 0.80
        assert policy.channel_unit_cost_inr.voice == 3.00
        assert "fraud" in policy.stop_keywords

    def test_defaults_used_when_file_absent(self, tmp_path: Path):
        policy = load_policy(tmp_path / "nonexistent.yaml")
        assert policy.max_retry_attempts == 3
        assert policy.anti_spam_cooldown_hours == 4
        assert policy.min_confidence_for_autonomous_action == 0.75

    def test_invalid_time_format_raises(self, tmp_path: Path):
        p = tmp_path / "bad_policy.yaml"
        p.write_text("quiet_hours_start: '25:99'\nquiet_hours_end: '09:00'\n")
        with pytest.raises(Exception):
            load_policy(p)

    def test_mutation_changes_loader_output(self, tmp_path: Path):
        """DoD item 4: changing policy.yaml changes the loader output."""
        p = tmp_path / "policy.yaml"
        p.write_text("max_retry_attempts: 3\n")

        policy1 = load_policy(p)
        assert policy1.max_retry_attempts == 3

        # Mutate the file
        p.write_text("max_retry_attempts: 7\n")

        # Force reload by clearing the cached mtime
        import app.policy as pm
        pm._policy = None
        pm._policy_mtime = 0.0

        policy2 = load_policy(p)
        assert policy2.max_retry_attempts == 7

    def test_repo_policy_yaml_is_valid(self):
        """Smoke test: the actual policy.yaml in the repo must be valid."""
        repo_root = Path(__file__).parent.parent
        policy_file = repo_root / "policy.yaml"
        if policy_file.exists():
            policy = load_policy(policy_file)
            assert policy.max_retry_attempts >= 1
            assert 0.0 <= policy.min_confidence_for_autonomous_action <= 1.0
