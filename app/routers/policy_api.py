"""
RevGuard — Live Policy Editor API (Differentiator #6)

Allows judges to tweak guardrail thresholds via a live POST endpoint and
see the changes take effect immediately — no redeploy required.

Exposes:
  GET  /policy        — Return the current loaded policy
  POST /policy/update — Patch one or more policy fields live
  POST /policy/reset  — Reset to the values in policy.yaml
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.logging_config import get_logger
import app.policy as _policy_module
from app.policy import get_policy, load_policy

router = APIRouter()
logger = get_logger(__name__)


class PolicyPatch(BaseModel):
    max_retry_attempts: Optional[int] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    anti_spam_cooldown_hours: Optional[float] = None
    min_confidence_for_autonomous_action: Optional[float] = None
    voice_call_min_amount_inr: Optional[float] = None


@router.get("/policy", tags=["policy"])
async def get_current_policy():
    """Return the currently active policy configuration."""
    policy = get_policy()
    return {
        "max_retry_attempts": policy.max_retry_attempts,
        "quiet_hours_start": policy.quiet_hours_start,
        "quiet_hours_end": policy.quiet_hours_end,
        "anti_spam_cooldown_hours": policy.anti_spam_cooldown_hours,
        "min_confidence_for_autonomous_action": policy.min_confidence_for_autonomous_action,
        "voice_call_min_amount_inr": policy.voice_call_min_amount_inr,
        "channel_unit_cost_inr": {
            "sms": policy.channel_unit_cost_inr.sms,
            "whatsapp": policy.channel_unit_cost_inr.whatsapp,
            "voice": policy.channel_unit_cost_inr.voice,
        },
    }


@router.post("/policy/update", tags=["policy"])
async def update_policy(patch: PolicyPatch):
    """
    Live patch one or more policy fields.  Changes take effect immediately
    for subsequent requests — no redeploy required.
    """
    policy = get_policy()
    changed: dict = {}

    if patch.max_retry_attempts is not None:
        if not 1 <= patch.max_retry_attempts <= 10:
            raise HTTPException(status_code=422, detail="max_retry_attempts must be 1–10")
        policy.max_retry_attempts = patch.max_retry_attempts
        changed["max_retry_attempts"] = patch.max_retry_attempts

    if patch.quiet_hours_start is not None:
        policy.quiet_hours_start = patch.quiet_hours_start
        changed["quiet_hours_start"] = patch.quiet_hours_start

    if patch.quiet_hours_end is not None:
        policy.quiet_hours_end = patch.quiet_hours_end
        changed["quiet_hours_end"] = patch.quiet_hours_end

    if patch.anti_spam_cooldown_hours is not None:
        if patch.anti_spam_cooldown_hours < 0:
            raise HTTPException(status_code=422, detail="anti_spam_cooldown_hours must be >= 0")
        policy.anti_spam_cooldown_hours = patch.anti_spam_cooldown_hours
        changed["anti_spam_cooldown_hours"] = patch.anti_spam_cooldown_hours

    if patch.min_confidence_for_autonomous_action is not None:
        if not 0.0 <= patch.min_confidence_for_autonomous_action <= 1.0:
            raise HTTPException(status_code=422, detail="confidence threshold must be 0.0–1.0")
        policy.min_confidence_for_autonomous_action = patch.min_confidence_for_autonomous_action
        changed["min_confidence_for_autonomous_action"] = patch.min_confidence_for_autonomous_action

    if patch.voice_call_min_amount_inr is not None:
        if patch.voice_call_min_amount_inr < 0:
            raise HTTPException(status_code=422, detail="voice_call_min_amount_inr must be >= 0")
        policy.voice_call_min_amount_inr = patch.voice_call_min_amount_inr
        changed["voice_call_min_amount_inr"] = patch.voice_call_min_amount_inr

    # Mutate the module-level singleton in place
    # (Policy is a Pydantic model but mutation works for live editing)
    _policy_module._policy = policy

    logger.info("policy.live_update", extra={"changed": changed})
    return {"status": "updated", "changed": changed, "policy": (await get_current_policy())}


@router.post("/policy/reset", tags=["policy"])
async def reset_policy():
    """Reset policy to the values stored in policy.yaml."""
    _policy_module._policy = None         # Force reload from file on next request
    _policy_module._policy_mtime = 0.0
    load_policy()   # Trigger immediate reload

    logger.info("policy.reset")
    return {"status": "reset", "policy": (await get_current_policy())}
