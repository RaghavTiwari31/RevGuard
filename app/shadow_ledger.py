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


@dataclass
class ShadowEntry:
    event_id: str
    amount_inr: float
    category: str
    action_type: str

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
        # Override recovery rates for testing
        revguard_rate: float | None = None,
        naive_rate: float | None = None,
    ) -> ShadowEntry:
        rv_rate = revguard_rate if revguard_rate is not None else _REVGUARD_RECOVERY_RATES.get(action_type, 0.0)
        naive_rate_ = naive_rate if naive_rate is not None else _NAIVE_RECOVERY_RATES.get(category, 0.0)

        entry = ShadowEntry(
            event_id=event_id,
            amount_inr=amount_inr,
            category=category,
            action_type=action_type,
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
