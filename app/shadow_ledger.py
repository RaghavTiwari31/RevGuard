"""
RevGuard — Do-Nothing Shadow Ledger (Differentiator #2)

For every payment failure event, simulates what would happen under the
naive "fixed-interval cron retry" approach (the status quo being replaced).

The naive approach: retry every failed payment after 24 hours, once, with
no intelligence about category, issuer health, or channel.

The shadow delta (RevGuard recovered − naive recovered) makes the ROI
pitch concrete and judge-visible.

Recovery probability assumptions (per category):
  Naive cron:
    TRANSIENT_DOWNTIME  → 40% recover (random bank recovery)
    TEMPORARY_CASHFLOW  → 15% recover (customer unlikely to have funds next day)
    EXPIRED_MANDATE     →  5% recover (mandate doesn't auto-renew)
    DISPUTE_OR_OPTOUT   →  0% recover (frozen)
    UNRECOVERABLE_FRAUD →  0% recover (frozen)

  RevGuard:
    TRANSIENT_DOWNTIME  → 75% recover (scheduled in optimal window)
    TEMPORARY_CASHFLOW  → 85% recover (direct payment link)
    EXPIRED_MANDATE     → 70% recover (mandate renewal link)
    ESCALATED (low conf)→ 30% recover (human follow-up)
    DISPUTE_OR_OPTOUT   →  0%
    UNRECOVERABLE_FRAUD →  0%
"""

from __future__ import annotations

from dataclasses import dataclass

from app.classifier import FailureCategory
from app.strategies.dispatcher import ActionType

# ── Recovery probability tables ───────────────────────────────────────────────

_REVGUARD_RECOVERY_RATES: dict[str, float] = {
    ActionType.SCHEDULE_RETRY.value: 0.75,
    ActionType.GENERATE_PAYMENT_LINK.value: 0.85,
    ActionType.SEND_MANDATE_LINK.value: 0.70,
    ActionType.ESCALATED_HUMAN_ATTENTION.value: 0.30,
    ActionType.DROPPED_NO_ACTION.value: 0.0,
}

_NAIVE_RECOVERY_RATES: dict[str, float] = {
    FailureCategory.TRANSIENT_DOWNTIME.value: 0.40,
    FailureCategory.TEMPORARY_CASHFLOW.value: 0.15,
    FailureCategory.EXPIRED_MANDATE.value: 0.05,
    FailureCategory.DISPUTE_OR_OPTOUT.value: 0.00,
    FailureCategory.UNRECOVERABLE_FRAUD.value: 0.00,
}


# ── Channel response model ────────────────────────────────────────────────────
# The recovery rates above describe the *action*.  They say nothing about the
# channel the outreach went out on — so using them directly as the bandit's
# reward gives every arm in a segment an identical score, and the bandit has
# nothing to learn.  These multipliers supply the missing dimension.
#
# Assumptions (India, dunning outreach):
#   WhatsApp — richest surface: the payment link renders as a tappable card and
#              costs the customer nothing to open.  Best baseline response.
#   SMS      — the link is bare text in a crowded inbox; reliably delivered but
#              materially lower click-through.
#   Voice    — no link at all, so it converts badly on small tickets.  On large
#              ones a live conversation outperforms every text channel, which is
#              why the payoff scales with the amount at stake.
_CHANNEL_ENGAGEMENT: dict[str, float] = {
    "whatsapp": 1.00,
    "sms": 0.85,
    "voice": 0.55,
    "none": 0.0,
}

# Above this ticket size a voice call is worth more than any text channel.
_VOICE_HIGH_VALUE_INR = 5_000.0
_VOICE_HIGH_VALUE_ENGAGEMENT = 1.15


def channel_engagement(channel: str, amount_inr: float) -> float:
    """
    Relative effectiveness of `channel` for a ticket of `amount_inr`.

    Deterministic, so a demo run is reproducible — same input, same number.
    """
    if channel == "voice" and amount_inr >= _VOICE_HIGH_VALUE_INR:
        return _VOICE_HIGH_VALUE_ENGAGEMENT
    return _CHANNEL_ENGAGEMENT.get(channel, 0.0)


def expected_recovery_rate(action_type: str, channel: str, amount_inr: float) -> float:
    """
    Probability that this (action, channel, amount) combination recovers the payment.

    This is what the bandit is scored on: the action sets the ceiling, the
    channel decides how much of it is actually realised.
    """
    base = _REVGUARD_RECOVERY_RATES.get(action_type, 0.0)
    return min(1.0, base * channel_engagement(channel, amount_inr))


@dataclass
class ShadowEntry:
    event_id: str
    amount_inr: float
    category: str
    action_type: str
    channel: str | None = None

    # Deterministic expected values (not random — reproducible for demos)
    revguard_recovered_inr: float = 0.0
    naive_recovered_inr: float = 0.0

    @property
    def delta_inr(self) -> float:
        return self.revguard_recovered_inr - self.naive_recovered_inr


class ShadowLedger:
    """Accumulates shadow ledger entries across a batch run."""

    def __init__(self):
        self.entries: list[ShadowEntry] = []

    def record(
        self,
        event_id: str,
        amount_inr: float,
        category: str,
        action_type: str,
        channel: str | None = None,
        # Override recovery rates for testing
        revguard_rate: float | None = None,
        naive_rate: float | None = None,
    ) -> ShadowEntry:
        """
        Record one event's expected outcome.

        When the action actually sent outreach, `channel` is folded into the
        recovery estimate.  The bandit is *scored* on channel-adjusted recovery,
        so the ledger has to measure the same thing — otherwise the reported
        recovery is blind to the very decision the bandit is making, and an A/B
        between channel strategies could only ever show a difference in cost.

        Actions that send nothing (a silent retry, a circuit-breaker freeze)
        have no channel and keep their base rate.
        """
        if revguard_rate is not None:
            rv_rate = revguard_rate
        elif channel and channel != "none":
            rv_rate = expected_recovery_rate(action_type, channel, amount_inr)
        else:
            rv_rate = _REVGUARD_RECOVERY_RATES.get(action_type, 0.0)

        naive_rate_ = naive_rate if naive_rate is not None else _NAIVE_RECOVERY_RATES.get(category, 0.0)

        entry = ShadowEntry(
            event_id=event_id,
            amount_inr=amount_inr,
            category=category,
            action_type=action_type,
            channel=channel,
            revguard_recovered_inr=round(amount_inr * rv_rate, 2),
            naive_recovered_inr=round(amount_inr * naive_rate_, 2),
        )
        self.entries.append(entry)
        return entry

    def summary(self) -> dict:
        total = sum(e.amount_inr for e in self.entries)
        revguard_total = sum(e.revguard_recovered_inr for e in self.entries)
        naive_total = sum(e.naive_recovered_inr for e in self.entries)
        delta = revguard_total - naive_total

        return {
            "total_amount_inr": round(total, 2),
            "revguard_recovered_inr": round(revguard_total, 2),
            "naive_recovered_inr": round(naive_total, 2),
            "delta_inr": round(delta, 2),
            "revguard_yield_pct": round(revguard_total / total * 100, 1) if total > 0 else 0.0,
            "naive_yield_pct": round(naive_total / total * 100, 1) if total > 0 else 0.0,
            "improvement_pct": round((revguard_total - naive_total) / total * 100, 1) if total > 0 else 0.0,
            "event_count": len(self.entries),
        }
