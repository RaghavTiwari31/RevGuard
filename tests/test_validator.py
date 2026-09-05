"""
Unit tests for the Post-Flight Safety Validator (app/validator.py).

DoD coverage:
  - Manually altering the LLM's proposed amount is rejected with HTTP 422.
  - Coercive/threatening tone is blocked and message sanitised.
"""

from __future__ import annotations

import pytest

from app.validator import check_amount, check_tone, run_post_flight


class TestAmountCheck:
    """DoD: Amount mismatch → hard failure (HTTP 422 upstream)."""

    def test_exact_match_passes(self):
        r = check_amount(expected_paise=149900, actual_paise=149900)
        assert r.passed is True

    def test_mismatch_fails(self):
        r = check_amount(expected_paise=149900, actual_paise=100000)
        assert r.passed is False
        assert "mismatch" in r.reason.lower()
        assert "₹1499.00" in r.reason

    def test_zero_amount_match(self):
        r = check_amount(expected_paise=0, actual_paise=0)
        assert r.passed is True

    def test_one_paise_difference_fails(self):
        r = check_amount(expected_paise=100, actual_paise=101)
        assert r.passed is False


class TestToneCheck:
    """Blocked patterns must be caught and message sanitised."""

    def test_clean_message_passes(self):
        r = check_tone("Namaste! Please complete your payment at your convenience. 🙏")
        assert r.passed is True
        assert r.sanitised_message == "Namaste! Please complete your payment at your convenience. 🙏"

    def test_legal_threat_blocked(self):
        r = check_tone("Pay now or we will take legal action against you.")
        assert r.passed is False
        assert "threat:legal_action" in r.blocked_patterns
        assert "legal action" not in r.sanitised_message

    def test_last_chance_ultimatum_blocked(self):
        r = check_tone("This is your last chance to pay before we escalate.")
        assert r.passed is False
        assert "coercive_ultimatum" in r.blocked_patterns

    def test_final_warning_blocked(self):
        r = check_tone("Final warning: pay immediately.")
        assert r.passed is False

    def test_blacklist_threat_blocked(self):
        r = check_tone("You will be blacklisted if you don't pay.")
        assert r.passed is False
        assert "threat:blacklist" in r.blocked_patterns

    def test_immediate_pay_blocked(self):
        r = check_tone("Please pay immediately to avoid issues.")
        assert r.passed is False

    def test_guarantee_blocked(self):
        r = check_tone("We guarantee a full refund if you pay now.")
        assert r.passed is False

    def test_unsubscribe_in_dunning_blocked(self):
        r = check_tone("To unsubscribe from this service, click here.")
        assert r.passed is False

    def test_hinglish_polite_message_passes(self):
        msg = (
            "Namaste! Aapka payment insufficient funds ki wajah se complete nahi hua. "
            "Neeche diye gaye link se convenient time par payment kar sakte hain. Shukriya! 😊"
        )
        r = check_tone(msg)
        assert r.passed is True

    def test_sanitised_message_is_safe_fallback(self):
        r = check_tone("Pay now or we will sue you.")
        assert r.passed is False
        assert r.sanitised_message != "Pay now or we will sue you."
        # Fallback message should not contain threats
        assert "legal" not in r.sanitised_message.lower()
        assert "sue" not in r.sanitised_message.lower()


class TestRunPostFlight:
    """Integration test for the combined post-flight check."""

    def test_all_pass(self):
        r = run_post_flight(
            expected_paise=100000,
            actual_paise=100000,
            outreach_message="Namaste! Please pay at your convenience. 🙏",
        )
        assert r.passed is True
        assert r.rejection_reason == ""

    def test_amount_mismatch_fails_overall(self):
        r = run_post_flight(
            expected_paise=100000,
            actual_paise=50000,
            outreach_message="Namaste! Please pay at your convenience. 🙏",
        )
        assert r.passed is False
        assert "mismatch" in r.rejection_reason.lower()

    def test_tone_failure_does_not_block_overall(self):
        """Tone failure sanitises message but does NOT set passed=False."""
        r = run_post_flight(
            expected_paise=100000,
            actual_paise=100000,
            outreach_message="Pay now or we will take legal action.",
        )
        # Amount matches → overall passes despite tone failure
        assert r.passed is True
        assert r.tone_check.passed is False
        assert "legal action" not in r.tone_check.sanitised_message
