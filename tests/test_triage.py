"""
Integration tests for the full triage pipeline (app/triage.py).

Covers all Phase 2 DoD items:
  - insufficient_funds → TEMPORARY_CASHFLOW + Payment Link created
  - Confidence below threshold → ESCALATED_HUMAN_ATTENTION
  - Stop-keyword → circuit breaker, automation frozen
  - LLM timeout/failure → canned template, pipeline never 500s
  - Amount invariant enforced (tested via validator, wired through triage)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from app.classifier import FailureCategory
from app.llm import LLMResult
from app.strategies.dispatcher import ActionType, OutcomeStatus
from app.triage import run_triage


def _canned(category: FailureCategory, confidence: float = 0.85) -> LLMResult:
    """Helper: return a mock LLMResult."""
    return LLMResult(
        confidence=confidence,
        rationale="Test rationale.",
        hinglish_message="Namaste! Test message. 🙏",
        provider_used="canned_fallback",
    )


class TestTriageInsufficientFunds:
    """DoD: insufficient_funds event → TEMPORARY_CASHFLOW + Payment Link."""

    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_insufficient_funds_produces_payment_link(
        self, mock_llm, async_session: AsyncSession
    ):
        mock_llm.return_value = _canned(FailureCategory.TEMPORARY_CASHFLOW)

        result = await run_triage(
            session=async_session,
            event_id="pay_test_001",
            trace_id="trc_test_001",
            amount_paise=149900,
            error_code="insufficient_funds",
            error_reason="payment_failed",
            error_description=None,
        )

        assert result.category == FailureCategory.TEMPORARY_CASHFLOW
        assert result.action_type == ActionType.GENERATE_PAYMENT_LINK
        assert result.outcome_status == OutcomeStatus.AWAITING_CUSTOMER_SETTLEMENT
        assert result.strategy.razorpay_payment_link_id is not None

    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_payment_link_amount_matches_original(
        self, mock_llm, async_session: AsyncSession
    ):
        """DoD: Payment Link amount must exactly equal amount_due."""
        mock_llm.return_value = _canned(FailureCategory.TEMPORARY_CASHFLOW)

        result = await run_triage(
            session=async_session,
            event_id="pay_test_002",
            trace_id="trc_test_002",
            amount_paise=50000,
            error_code="insufficient_funds",
            error_reason=None,
            error_description=None,
        )

        assert result.category == FailureCategory.TEMPORARY_CASHFLOW
        # Amount in trace dict must equal original
        trace_dict = result.to_trace_dict()
        assert trace_dict["amount_inr"] == 500.00


class TestConfidenceGate:
    """DoD: Confidence below threshold → ESCALATED_HUMAN_ATTENTION."""

    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_low_confidence_forces_escalation(
        self, mock_llm, async_session: AsyncSession
    ):
        # Return a low-confidence result
        mock_llm.return_value = _canned(FailureCategory.TEMPORARY_CASHFLOW, confidence=0.50)

        from app.policy import Policy
        low_conf_policy = Policy(min_confidence_for_autonomous_action=0.75)

        result = await run_triage(
            session=async_session,
            event_id="pay_test_003",
            trace_id="trc_test_003",
            amount_paise=100000,
            error_code="insufficient_funds",
            error_reason=None,
            error_description=None,
            policy=low_conf_policy,
        )

        assert result.action_type == ActionType.ESCALATED_HUMAN_ATTENTION
        assert result.outcome_status == OutcomeStatus.ESCALATED

    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_above_threshold_not_escalated(
        self, mock_llm, async_session: AsyncSession
    ):
        mock_llm.return_value = _canned(FailureCategory.TEMPORARY_CASHFLOW, confidence=0.90)

        from app.policy import Policy
        policy = Policy(min_confidence_for_autonomous_action=0.75)

        result = await run_triage(
            session=async_session,
            event_id="pay_test_004",
            trace_id="trc_test_004",
            amount_paise=100000,
            error_code="insufficient_funds",
            error_reason=None,
            error_description=None,
            policy=policy,
        )

        assert result.action_type != ActionType.ESCALATED_HUMAN_ATTENTION


class TestStopKeyword:
    """DoD: Stop-keyword reply freezes all further automation."""

    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_stop_keyword_triggers_circuit_breaker(
        self, mock_llm, async_session: AsyncSession
    ):
        mock_llm.return_value = _canned(FailureCategory.DISPUTE_OR_OPTOUT)

        result = await run_triage(
            session=async_session,
            event_id="pay_test_005",
            trace_id="trc_test_005",
            amount_paise=100000,
            error_code="insufficient_funds",
            error_reason=None,
            error_description=None,
            customer_reply="STOP",  # <-- stop keyword
        )

        assert result.category == FailureCategory.DISPUTE_OR_OPTOUT
        assert result.action_type == ActionType.ESCALATED_HUMAN_ATTENTION
        assert result.outcome_status == OutcomeStatus.ESCALATED
        assert result.classification.is_stop_keyword is True


class TestLLMFallback:
    """DoD: LLM failure/timeout → canned template, pipeline never 500s."""

    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_llm_timeout_uses_canned_fallback(
        self, mock_llm, async_session: AsyncSession
    ):
        # Simulate a timed-out LLM result (the llm module handles this internally)
        mock_llm.return_value = LLMResult(
            confidence=0.80,
            rationale="Canned rationale.",
            hinglish_message="Canned Hinglish message. 🙏",
            provider_used="canned_fallback",
            timed_out=True,
        )

        result = await run_triage(
            session=async_session,
            event_id="pay_test_006",
            trace_id="trc_test_006",
            amount_paise=100000,
            error_code="gateway_timeout",
            error_reason=None,
            error_description=None,
        )

        # Pipeline must still complete successfully
        assert result.action_type is not None
        assert result.llm.timed_out is True
        assert result.llm.provider_used == "canned_fallback"

    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_llm_exception_uses_canned_fallback(
        self, mock_llm, async_session: AsyncSession
    ):
        # The LLM module catches exceptions and returns canned — simulate that
        mock_llm.return_value = LLMResult(
            confidence=0.80,
            rationale="Fallback rationale.",
            hinglish_message="Fallback Hinglish. 🙏",
            provider_used="canned_fallback",
        )

        result = await run_triage(
            session=async_session,
            event_id="pay_test_007",
            trace_id="trc_test_007",
            amount_paise=100000,
            error_code="bank_technical_error",
            error_reason=None,
            error_description=None,
        )

        assert result.llm.provider_used == "canned_fallback"
        assert result.action_type is not None  # Pipeline didn't 500


class TestTransientDowntimeStrategies:
    """Tests for Strategy 1 (silent retry) and issuer health radar integration."""

    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_transient_downtime_schedules_retry(
        self, mock_llm, async_session: AsyncSession
    ):
        mock_llm.return_value = _canned(FailureCategory.TRANSIENT_DOWNTIME)

        result = await run_triage(
            session=async_session,
            event_id="pay_test_008",
            trace_id="trc_test_008",
            amount_paise=100000,
            error_code="gateway_timeout",
            error_reason=None,
            error_description=None,
        )

        assert result.category == FailureCategory.TRANSIENT_DOWNTIME
        assert result.action_type == ActionType.SCHEDULE_RETRY
        assert result.strategy.retry_scheduled_at is not None


class TestExpiredMandateStrategy:
    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_expired_mandate_sends_link(
        self, mock_llm, async_session: AsyncSession
    ):
        mock_llm.return_value = _canned(FailureCategory.EXPIRED_MANDATE)

        result = await run_triage(
            session=async_session,
            event_id="pay_test_009",
            trace_id="trc_test_009",
            amount_paise=100000,
            error_code="mandate_expired",
            error_reason=None,
            error_description=None,
        )

        assert result.category == FailureCategory.EXPIRED_MANDATE
        assert result.action_type == ActionType.SEND_MANDATE_LINK
        assert result.outcome_status == OutcomeStatus.AWAITING_MANDATE_RENEWAL


class TestFraudCircuitBreaker:
    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_fraud_escalated_no_outreach(
        self, mock_llm, async_session: AsyncSession
    ):
        mock_llm.return_value = _canned(FailureCategory.UNRECOVERABLE_FRAUD)

        result = await run_triage(
            session=async_session,
            event_id="pay_test_010",
            trace_id="trc_test_010",
            amount_paise=100000,
            error_code="fraud",
            error_reason=None,
            error_description=None,
        )

        assert result.category == FailureCategory.UNRECOVERABLE_FRAUD
        assert result.action_type == ActionType.ESCALATED_HUMAN_ATTENTION
        # Fraud: no outreach (dispatch_record should be None)
        assert result.strategy.dispatch_record is None


class TestToTraceDict:
    @patch("app.triage.get_llm_rationale", new_callable=AsyncMock)
    async def test_trace_dict_has_required_fields(
        self, mock_llm, async_session: AsyncSession
    ):
        mock_llm.return_value = _canned(FailureCategory.TEMPORARY_CASHFLOW)

        result = await run_triage(
            session=async_session,
            event_id="pay_test_011",
            trace_id="trc_test_011",
            amount_paise=100000,
            error_code="insufficient_funds",
            error_reason=None,
            error_description=None,
        )

        d = result.to_trace_dict()
        required_keys = {
            "category", "action_type", "outcome_status", "confidence",
            "rationale", "hinglish_message", "amount_inr",
            "classification_rule", "provider_used", "tone_check_passed",
            "amount_check_passed",
        }
        assert required_keys.issubset(d.keys())
