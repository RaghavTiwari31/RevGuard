"""
Tests for the channel response model that feeds the bandit's reward signal.

The bug this pins down: the reward used to be looked up purely by action type.
Because a segment maps one-to-one onto an action, every arm in a segment scored
identically and the bandit was mathematically incapable of learning anything —
it reported "adaptive" behaviour while consuming a constant.
"""

from __future__ import annotations

from app.shadow_ledger import (
    ShadowLedger,
    channel_engagement,
    expected_recovery_rate,
)
from app.strategies.dispatcher import ActionType

PAYMENT_LINK = ActionType.GENERATE_PAYMENT_LINK.value
MANDATE_LINK = ActionType.SEND_MANDATE_LINK.value


class TestChannelEngagement:
    def test_channels_are_not_interchangeable(self):
        """The whole premise of the bandit: the arms must differ."""
        small = 1_000.0
        rates = {
            channel_engagement(c, small) for c in ("sms", "whatsapp", "voice")
        }
        assert len(rates) == 3

    def test_whatsapp_beats_sms_on_a_small_ticket(self):
        assert channel_engagement("whatsapp", 1_000) > channel_engagement("sms", 1_000)

    def test_voice_is_weak_on_small_tickets_and_strong_on_large_ones(self):
        assert channel_engagement("voice", 500) < channel_engagement("whatsapp", 500)
        assert channel_engagement("voice", 50_000) > channel_engagement("whatsapp", 50_000)

    def test_no_outreach_earns_nothing(self):
        assert channel_engagement("none", 10_000) == 0.0
        assert channel_engagement("carrier-pigeon", 10_000) == 0.0

    def test_is_deterministic(self):
        assert channel_engagement("voice", 7_500) == channel_engagement("voice", 7_500)


class TestExpectedRecoveryRate:
    def test_reward_varies_by_channel_for_one_action(self):
        rates = {
            expected_recovery_rate(PAYMENT_LINK, c, 1_000)
            for c in ("sms", "whatsapp", "voice")
        }
        assert len(rates) == 3, "bandit arms must be distinguishable"

    def test_action_sets_the_ceiling(self):
        """A weaker action cannot outscore a stronger one on the same channel."""
        assert expected_recovery_rate(PAYMENT_LINK, "whatsapp", 1_000) > expected_recovery_rate(
            MANDATE_LINK, "whatsapp", 1_000
        )

    def test_rate_stays_a_probability(self):
        for action in (PAYMENT_LINK, MANDATE_LINK):
            for channel in ("sms", "whatsapp", "voice", "none"):
                for amount in (100, 5_000, 10_000_000):
                    rate = expected_recovery_rate(action, channel, amount)
                    assert 0.0 <= rate <= 1.0

    def test_unknown_action_scores_zero(self):
        assert expected_recovery_rate("NOT_AN_ACTION", "whatsapp", 1_000) == 0.0


class TestShadowLedgerSummary:
    def test_summary_is_consistent(self):
        ledger = ShadowLedger()
        ledger.record("e1", 10_000, "TEMPORARY_CASHFLOW", PAYMENT_LINK)
        ledger.record("e2", 5_000, "EXPIRED_MANDATE", MANDATE_LINK)

        summary = ledger.summary()
        assert summary["event_count"] == 2
        assert summary["total_amount_inr"] == 15_000
        assert summary["revguard_recovered_inr"] > summary["naive_recovered_inr"]
        assert summary["delta_inr"] == round(
            summary["revguard_recovered_inr"] - summary["naive_recovered_inr"], 2
        )

    def test_empty_ledger_does_not_divide_by_zero(self):
        summary = ShadowLedger().summary()
        assert summary["revguard_yield_pct"] == 0.0
        assert summary["event_count"] == 0
