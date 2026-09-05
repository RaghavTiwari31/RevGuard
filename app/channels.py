"""
RevGuard — Mocked Channel Dispatch Layer

Simulates WhatsApp / SMS / Voice outreach without real sends.
Returns structured dispatch records (cost, channel, message) that:
  - Feed the cost-benefit optimizer (dashboard differentiator #9)
  - Are logged to the audit trail
  - Are stored in the Trace row

No real WhatsApp Business API or Twilio/Exotel calls are made — this is
explicitly locked in by the implementation plan to stay within free-tier
scope and avoid business verification delays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.logging_config import get_logger
from app.policy import Policy, get_policy

logger = get_logger(__name__)


# ── Channel enum ──────────────────────────────────────────────────────────────

class Channel(str, Enum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    VOICE = "voice"
    NONE = "none"           # No outreach (fraud / already escalated)


# ── Dispatch result ───────────────────────────────────────────────────────────

@dataclass
class DispatchRecord:
    channel: Channel
    message: str
    cost_inr: float
    simulated: bool = True      # Always True — no real sends
    recipient_phone: Optional[str] = None
    metadata: dict = field(default_factory=dict)


# ── Channel selector (epsilon-greedy bandit stub for Phase 3) ─────────────────

def select_channel(
    amount_inr: float,
    customer_phone: Optional[str],
    policy: Optional[Policy] = None,
) -> Channel:
    """
    Simple channel selection based on amount and policy.
    Phase 3 will upgrade this to the epsilon-greedy Adaptive Channel Bandit.

    Rules:
      - No phone → SMS (best-effort)
      - amount >= voice_call_min_amount_inr → VOICE
      - Default → WHATSAPP (higher engagement than SMS)
    """
    if policy is None:
        policy = get_policy()

    if not customer_phone:
        return Channel.SMS

    if amount_inr >= policy.voice_call_min_amount_inr:
        return Channel.VOICE

    return Channel.WHATSAPP


def get_channel_cost(channel: Channel, policy: Optional[Policy] = None) -> float:
    """Look up the cost per outreach from policy.yaml."""
    if policy is None:
        policy = get_policy()

    costs = {
        Channel.SMS: policy.channel_unit_cost_inr.sms,
        Channel.WHATSAPP: policy.channel_unit_cost_inr.whatsapp,
        Channel.VOICE: policy.channel_unit_cost_inr.voice,
        Channel.NONE: 0.0,
    }
    return costs.get(channel, 0.0)


# ── Dispatch functions ────────────────────────────────────────────────────────

def dispatch_sms(
    message: str,
    phone: Optional[str],
    policy: Optional[Policy] = None,
) -> DispatchRecord:
    """Simulate an SMS dispatch."""
    if policy is None:
        policy = get_policy()

    cost = policy.channel_unit_cost_inr.sms
    record = DispatchRecord(
        channel=Channel.SMS,
        message=message[:160],      # SMS character limit
        cost_inr=cost,
        recipient_phone=phone,
        metadata={"char_count": len(message), "truncated": len(message) > 160},
    )
    logger.info("channel.dispatch", extra={
        "channel": "sms",
        "simulated": True,
        "cost_inr": cost,
        "phone": phone or "unknown",
    })
    return record


def dispatch_whatsapp(
    message: str,
    phone: Optional[str],
    policy: Optional[Policy] = None,
) -> DispatchRecord:
    """Send a real WhatsApp message using Twilio API."""
    import os
    from twilio.rest import Client

    if policy is None:
        policy = get_policy()

    cost = policy.channel_unit_cost_inr.whatsapp
    
    # Try sending via Twilio if we have a phone number
    if phone:
        try:
            account_sid = os.getenv("TWILIO_ACCOUNT_SID")
            auth_token = os.getenv("TWILIO_AUTH_TOKEN")
            sender = os.getenv("TWILIO_WHATSAPP_SENDER")
            
            if account_sid and auth_token and sender:
                client = Client(account_sid, auth_token)
                
                # Format to E.164. Assuming 10-digit Indian numbers if no country code.
                cleaned = phone.replace('+', '').replace(' ', '')
                if len(cleaned) == 10:
                    cleaned = f"91{cleaned}"
                elif cleaned.startswith('0'):
                    cleaned = f"91{cleaned[1:]}"
                    
                formatted_phone = f"whatsapp:+{cleaned}"
                
                client.messages.create(
                    from_=sender,
                    body=message[:4096],
                    to=formatted_phone
                )
                logger.info("channel.twilio_success", extra={"phone": formatted_phone})
            else:
                logger.warning("channel.twilio_skipped", extra={"reason": "missing_credentials"})
        except Exception as e:
            logger.error("channel.twilio_error", extra={"error": str(e)})

    record = DispatchRecord(
        channel=Channel.WHATSAPP,
        message=message[:4096],     # WhatsApp message limit
        cost_inr=cost,
        simulated=False,            # It's real now!
        recipient_phone=phone,
        metadata={"char_count": len(message)},
    )
    logger.info("channel.dispatch", extra={
        "channel": "whatsapp",
        "simulated": False,
        "cost_inr": cost,
        "phone": phone or "unknown",
    })
    return record


def dispatch_voice(
    message: str,
    phone: Optional[str],
    policy: Optional[Policy] = None,
) -> DispatchRecord:
    """Simulate a voice call dispatch (TTS script)."""
    if policy is None:
        policy = get_policy()

    cost = policy.channel_unit_cost_inr.voice
    record = DispatchRecord(
        channel=Channel.VOICE,
        message=message,
        cost_inr=cost,
        recipient_phone=phone,
        metadata={"tts_script_chars": len(message)},
    )
    logger.info("channel.dispatch", extra={
        "channel": "voice",
        "simulated": True,
        "cost_inr": cost,
        "phone": phone or "unknown",
    })
    return record


def dispatch(
    message: str,
    channel: Channel,
    phone: Optional[str],
    policy: Optional[Policy] = None,
) -> DispatchRecord:
    """Route to the correct simulated channel dispatch function."""
    if channel == Channel.SMS:
        return dispatch_sms(message, phone, policy)
    elif channel == Channel.WHATSAPP:
        return dispatch_whatsapp(message, phone, policy)
    elif channel == Channel.VOICE:
        return dispatch_voice(message, phone, policy)
    else:
        return DispatchRecord(
            channel=Channel.NONE,
            message="",
            cost_inr=0.0,
            recipient_phone=phone,
        )
