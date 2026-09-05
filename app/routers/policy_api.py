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

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

import app.policy as _policy_module
from app.logging_config import get_logger
from app.policy import Policy, get_policy, load_policy

router = APIRouter()
logger = get_logger(__name__)


class PolicyPatch(BaseModel):
    max_retry_attempts: Optional[int] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None
    anti_spam_cooldown_hours: Optional[float] = None
    min_confidence_for_autonomous_action: Optional[float] = None
    voice_call_min_amount_inr: Optional[float] = None
    enable_adaptive_channel_bandit: Optional[bool] = None


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
        "enable_adaptive_channel_bandit": policy.enable_adaptive_channel_bandit,
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

    if patch.max_retry_attempts is not None and not 1 <= patch.max_retry_attempts <= 10:
        raise HTTPException(status_code=422, detail="max_retry_attempts must be 1–10")

    changed = patch.model_dump(exclude_none=True)
    if not changed:
        raise HTTPException(status_code=422, detail="No policy fields supplied")

    # Build the candidate policy as a whole and validate it before publishing.
    # An invalid patch must leave the live policy completely untouched — a
    # half-applied guardrail config is worse than a rejected one.
    try:
        candidate = policy.model_copy(update=changed)
        candidate = Policy.model_validate(candidate.model_dump())
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
            for e in exc.errors()
        ]
        logger.warning("policy.update_rejected", extra={"errors": errors})
        raise HTTPException(status_code=422, detail="; ".join(errors)) from exc

    # Publish atomically — swap the singleton only once the candidate is valid.
    _policy_module._policy = candidate

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
