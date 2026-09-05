"""
Unit tests for the deterministic classifier (app/classifier.py).

Covers all five categories and the classification priority order.
"""

from __future__ import annotations

from app.classifier import FailureCategory, classify


class TestTransientDowntime:
    def test_gateway_timeout(self):
        r = classify("gateway_timeout", None, None)
        assert r.category == FailureCategory.TRANSIENT_DOWNTIME

    def test_bank_technical_error(self):
        r = classify("bank_technical_error", None, None)
        assert r.category == FailureCategory.TRANSIENT_DOWNTIME

    def test_unknown_code_defaults_to_transient(self):
        r = classify("some_unknown_code_xyz", None, None)
        assert r.category == FailureCategory.TRANSIENT_DOWNTIME
        assert r.matched_rule == "default_fallback"

    def test_empty_inputs_default_to_transient(self):
        r = classify(None, None, None)
        assert r.category == FailureCategory.TRANSIENT_DOWNTIME

    def test_bad_gateway_code(self):
        r = classify("bad_gateway", None, None)
        assert r.category == FailureCategory.TRANSIENT_DOWNTIME


class TestTemporaryCashflow:
    def test_insufficient_funds(self):
        r = classify("insufficient_funds", None, None)
        assert r.category == FailureCategory.TEMPORARY_CASHFLOW
        assert r.confidence_hint == 1.0

    def test_do_not_honor(self):
        r = classify("do_not_honor", None, None)
        assert r.category == FailureCategory.TEMPORARY_CASHFLOW

    def test_credit_limit_exceeded(self):
        r = classify("credit_limit_exceeded", None, None)
        assert r.category == FailureCategory.TEMPORARY_CASHFLOW

    def test_reason_pattern_funds(self):
        r = classify("BAD_REQUEST_ERROR", "insufficient balance", None)
        assert r.category == FailureCategory.TEMPORARY_CASHFLOW


class TestExpiredMandate:
    def test_mandate_expired(self):
        r = classify("mandate_expired", None, None)
        assert r.category == FailureCategory.EXPIRED_MANDATE
        assert r.confidence_hint == 1.0

    def test_mandate_revoked(self):
        r = classify("mandate_revoked", None, None)
        assert r.category == FailureCategory.EXPIRED_MANDATE

    def test_subscription_cancelled(self):
        r = classify("subscription_cancelled", None, None)
        assert r.category == FailureCategory.EXPIRED_MANDATE

    def test_token_expired(self):
        r = classify("token_expired", None, None)
        assert r.category == FailureCategory.EXPIRED_MANDATE

    def test_reason_pattern_nach(self):
        r = classify("BAD_REQUEST_ERROR", "nach_debit_failed", None)
        assert r.category == FailureCategory.EXPIRED_MANDATE


class TestDisputeOrOptout:
    """Stop-keyword detection has highest priority."""

    def test_stop_keyword_in_reply(self):
        r = classify(None, None, None, customer_reply="STOP")
        assert r.category == FailureCategory.DISPUTE_OR_OPTOUT
        assert r.is_stop_keyword is True

    def test_unsubscribe_in_reply(self):
        r = classify(None, None, None, customer_reply="please unsubscribe me")
        assert r.category == FailureCategory.DISPUTE_OR_OPTOUT

    def test_unauthorized_in_description(self):
        r = classify(None, None, "This transaction is unauthorized")
        assert r.category == FailureCategory.DISPUTE_OR_OPTOUT

    def test_stop_keyword_overrides_fraud_code(self):
        """DISPUTE_OR_OPTOUT has higher priority than UNRECOVERABLE_FRAUD."""
        r = classify("fraud", None, None, customer_reply="unsubscribe")
        assert r.category == FailureCategory.DISPUTE_OR_OPTOUT

    def test_stop_keyword_overrides_cashflow(self):
        r = classify("insufficient_funds", None, None, customer_reply="stop")
        assert r.category == FailureCategory.DISPUTE_OR_OPTOUT


class TestUnrecoverableFraud:
    def test_fraud_code(self):
        r = classify("fraud", None, None)
        assert r.category == FailureCategory.UNRECOVERABLE_FRAUD
        assert r.confidence_hint == 1.0

    def test_risk_threshold(self):
        r = classify("risk_threshold", None, None)
        assert r.category == FailureCategory.UNRECOVERABLE_FRAUD

    def test_fraud_overrides_cashflow(self):
        """Fraud check is higher priority than cashflow."""
        r = classify("fraud", "insufficient_funds", None)
        # stop-keyword check first, then fraud — no stop keyword here
        assert r.category == FailureCategory.UNRECOVERABLE_FRAUD


class TestPriority:
    """Explicit priority order validation."""

    def test_cashflow_before_transient(self):
        r = classify("insufficient_funds", "gateway_timeout", None)
        # Cashflow code is exact match → wins over transient reason
        assert r.category == FailureCategory.TEMPORARY_CASHFLOW

    def test_mandate_before_cashflow(self):
        r = classify("mandate_expired", "insufficient", None)
        assert r.category == FailureCategory.EXPIRED_MANDATE
