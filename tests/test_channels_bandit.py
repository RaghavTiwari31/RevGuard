"""
Regression tests for channel eligibility, cost-aware selection, and the
epsilon-greedy bandit.

Each test here pins down a bug that was live in the codebase:
  - voice was selected for every ticket over ₹100, at 6× the WhatsApp cost
  - the bandit was never consulted for selection at all
  - an arm that had never been measured won exploitation forever
  - warm-up re-selected the same arm indefinitely because selections were
    only counted when a reward arrived
"""

from __future__ import annotations

from collections import Counter

from app.bandit import (
    MIN_TRIALS,
    get_bandit_stats,
    record_reward,
    reset_bandit,
    select_channel_bandit,
)
from app.channels import Channel, eligible_channels, get_channel_cost, select_channel
from app.classifier import FailureCategory
from app.policy import Policy
from app.strategies.dispatcher import choose_channel


def _policy(**overrides) -> Policy:
    return Policy(**overrides)


# ── Eligibility ───────────────────────────────────────────────────────────────

class TestEligibility:
    def test_no_phone_leaves_only_sms(self):
        assert eligible_channels(50_000, None, _policy()) == [Channel.SMS]

    def test_voice_floor_is_enforced(self):
        policy = _policy(voice_call_min_amount_inr=100)
        assert Channel.VOICE not in eligible_channels(99, "+919000000000", policy)
        assert Channel.VOICE in eligible_channels(100, "+919000000000", policy)

    def test_eligible_channels_are_cheapest_first(self):
        policy = _policy()
        channels = eligible_channels(10_000, "+919000000000", policy)
        costs = [get_channel_cost(c, policy) for c in channels]
        assert costs == sorted(costs)


# ── Deterministic cost-aware selection ────────────────────────────────────────

class TestSelectChannel:
    def test_small_ticket_does_not_get_a_voice_call(self):
        """
        Clearing the eligibility floor must not by itself trigger the most
        expensive channel.  A ₹150 ticket is voice-eligible but nowhere near
        worth a ₹2.50 call.
        """
        policy = _policy(voice_call_min_amount_inr=100)
        assert Channel.VOICE in eligible_channels(150, "+919000000000", policy)
        assert select_channel(150, "+919000000000", policy) == Channel.WHATSAPP

    def test_large_ticket_justifies_a_voice_call(self):
        policy = _policy()
        assert select_channel(50_000, "+919000000000", policy) == Channel.VOICE

    def test_no_phone_falls_back_to_sms(self):
        assert select_channel(50_000, None, _policy()) == Channel.SMS

    def test_selection_never_leaves_the_eligible_set(self):
        policy = _policy()
        for amount in (10, 99, 100, 4_999, 5_000, 100_000):
            chosen = select_channel(amount, "+919000000000", policy)
            assert chosen in eligible_channels(amount, "+919000000000", policy)


# ── Bandit ────────────────────────────────────────────────────────────────────

class TestBandit:
    def setup_method(self):
        reset_bandit()

    def test_warmup_spreads_across_arms_before_any_reward(self):
        """
        Selections must be counted at selection time.  If warm-up keyed off
        rewards instead, the first arm would be picked every time until a
        reward happened to arrive.
        """
        arms = [Channel.SMS, Channel.WHATSAPP, Channel.VOICE]
        picks = Counter(
            select_channel_bandit("SEG", arms, epsilon=0.0).value
            for _ in range(MIN_TRIALS * len(arms))
        )
        assert picks == {c.value: MIN_TRIALS for c in arms}

    def test_unmeasured_arm_does_not_win_exploitation(self):
        """
        An arm with no observations used to score `inf` and beat every proven
        arm forever.  Once warm-up is satisfied, the best *observed* arm wins.
        """
        arms = [Channel.SMS, Channel.WHATSAPP, Channel.VOICE]

        # Satisfy warm-up, then teach it that SMS works and the others do not.
        for _ in range(MIN_TRIALS):
            for channel in arms:
                select_channel_bandit("SEG", arms, epsilon=0.0)
        for _ in range(10):
            record_reward("SEG", Channel.SMS, 1.0)
            record_reward("SEG", Channel.WHATSAPP, 0.0)
            record_reward("SEG", Channel.VOICE, 0.0)

        picks = Counter(
            select_channel_bandit("SEG", arms, epsilon=0.0).value for _ in range(20)
        )
        assert picks == {Channel.SMS.value: 20}

    def test_bandit_never_picks_an_ineligible_arm(self):
        """The bandit chooses within policy; it can never learn around a guardrail."""
        eligible = [Channel.SMS, Channel.WHATSAPP]
        for _ in range(50):
            assert select_channel_bandit("SEG", eligible) in eligible

    def test_empty_eligibility_yields_no_channel(self):
        assert select_channel_bandit("SEG", []) == Channel.NONE

    def test_stats_expose_selections_and_rewards_separately(self):
        select_channel_bandit("SEG", [Channel.SMS, Channel.WHATSAPP], epsilon=0.0)
        stats = get_bandit_stats()["SEG"]
        assert stats["sms"]["selections"] == 1
        assert stats["sms"]["pulls"] == 0       # no reward observed yet

        record_reward("SEG", Channel.SMS, 1.0)
        stats = get_bandit_stats()["SEG"]
        assert stats["sms"]["pulls"] == 1
        assert stats["sms"]["mean_reward"] == 1.0


# ── Dispatcher wiring ─────────────────────────────────────────────────────────

class TestChooseChannel:
    def setup_method(self):
        reset_bandit()

    def test_bandit_is_actually_consulted_when_enabled(self):
        """
        The bandit used to be imported but never called — every channel came
        from the deterministic selector.  With it enabled, selections must be
        recorded in bandit state.
        """
        policy = _policy(enable_adaptive_channel_bandit=True)
        for _ in range(6):
            choose_channel(
                FailureCategory.TEMPORARY_CASHFLOW.value, 8_000, "+919000000000", policy
            )

        stats = get_bandit_stats()[FailureCategory.TEMPORARY_CASHFLOW.value]
        assert sum(arm["selections"] for arm in stats.values()) == 6

    def test_disabling_the_bandit_falls_back_to_deterministic_selection(self):
        policy = _policy(enable_adaptive_channel_bandit=False)
        chosen = choose_channel(
            FailureCategory.TEMPORARY_CASHFLOW.value, 50_000, "+919000000000", policy
        )
        assert chosen == select_channel(50_000, "+919000000000", policy)
        assert get_bandit_stats() == {}     # bandit was never touched

    def test_choice_always_respects_policy_eligibility(self):
        policy = _policy(voice_call_min_amount_inr=10_000)
        for _ in range(30):
            chosen = choose_channel(
                FailureCategory.TEMPORARY_CASHFLOW.value, 500, "+919000000000", policy
            )
            assert chosen != Channel.VOICE
