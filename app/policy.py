"""
RevGuard — Policy-as-Code Loader

Loads policy.yaml at startup (validated via Pydantic) and exposes a singleton
`policy` object.  Every guardrail check reads from this object — no hardcoded
thresholds anywhere in the pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

import yaml
from pydantic import BaseModel, Field, field_validator


class ChannelCosts(BaseModel):
    sms: float = 0.20
    whatsapp: float = 0.40
    voice: float = 2.50


class Policy(BaseModel):
    """All guardrail thresholds, read from policy.yaml."""

    # Re-run validators on attribute assignment.  The live policy editor
    # (POST /policy/update) mutates this singleton in place, so without this
    # a bad value would be accepted here and only blow up later, deep inside
    # a guardrail check.  Validate at the boundary instead.
    model_config = {"validate_assignment": True}

    # Retry / lifecycle
    max_retry_attempts: int = Field(default=3, ge=1)

    # Anti-spam / quiet-hours
    anti_spam_cooldown_hours: float = Field(default=4.0, ge=0)
    quiet_hours_start: str = "21:00"  # HH:MM in the configured timezone
    quiet_hours_end: str = "09:00"
    timezone: str = "Asia/Kolkata"

    # LLM confidence gate
    min_confidence_for_autonomous_action: float = Field(default=0.75, ge=0.0, le=1.0)

    # Channel eligibility
    voice_call_min_amount_inr: float = Field(default=100.0, ge=0)

    # Adaptive channel selection.
    #
    # Default is OFF, and that is a measured decision rather than a preference:
    # POST /simulate/ab runs both strategies over identical data, and the
    # deterministic selector wins on this workload by roughly Rs 9k per 100
    # records even after the bandit is pre-trained. The deterministic rule
    # already encodes the structure the reward model rewards (voice pays for
    # itself above the high-value threshold, WhatsApp otherwise), so the bandit
    # can only rediscover it — at the cost of exploring arms it will reject.
    #
    # Set to true to run the bandit in production; it is fully implemented,
    # persisted across restarts, and re-measurable at any time via /simulate/ab.
    enable_adaptive_channel_bandit: bool = False

    # Idempotency lock lifetime.  A lock is claimed before triage runs, so an
    # incomplete lock left by a crash or a free-tier spin-down must eventually
    # be reclaimable — otherwise the event ispermanently un-processable.
    # Completed locks never expire.
    idempotency_lock_ttl_minutes: float = Field(default=15.0, gt=0)

    # Durable retry queue
    enable_scheduled_retries: bool = True
    # Retries whose due time passed while the service was asleep are run this
    # many seconds after boot, staggered, rather than all at once.
    retry_catchup_delay_seconds: float = Field(default=20.0, ge=0)
    # Hard ceiling on how far ahead a retry may be scheduled.
    max_retry_delay_minutes: float = Field(default=720.0, gt=0)

    # Cost-benefit optimizer
    channel_unit_cost_inr: ChannelCosts = Field(default_factory=ChannelCosts)

    # Tone / compliance filter
    stop_keywords: List[str] = Field(
        default_factory=lambda: ["unauthorized", "fraud", "unsubscribe", "stop"]
    )

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def validate_hhmm(cls, v: str) -> str:
        try:
            h, m = v.split(":")
            assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
        except Exception:
            raise ValueError(f"Time must be in HH:MM format, got: {v!r}")
        return v


# ── Module-level singleton ────────────────────────────────────────────────────

_policy: Policy | None = None
_policy_mtime: float = 0.0
_policy_path: Path | None = None


def load_policy(path: str | Path | None = None) -> Policy:
    """
    Load (or reload) the policy file.  Call once at startup; subsequent calls
    are idempotent unless the file has changed on disk (hot-reload friendly).
    """
    global _policy, _policy_mtime, _policy_path

    if path is None:
        path = Path(os.getenv("POLICY_FILE", "policy.yaml"))
    path = Path(path)
    _policy_path = path

    mtime = path.stat().st_mtime if path.exists() else 0.0

    if _policy is not None and mtime == _policy_mtime:
        return _policy  # Nothing changed — return cached singleton

    if not path.exists():
        # Graceful fallback: use defaults when the file is absent (e.g. CI)
        _policy = Policy()
        _policy_mtime = 0.0
        return _policy

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    _policy = Policy(**raw)
    _policy_mtime = mtime
    return _policy


def get_policy() -> Policy:
    """Return the cached singleton, loading it if never called before."""
    if _policy is None:
        return load_policy()
    return _policy
