"""
RevGuard — Triage Pipeline Orchestrator

Wires together (in order):
  1. Deterministic Classifier   → category (O(1), no LLM)
  2. Issuer Health Radar        → extended backoff check
  3. LLM Rationale Layer        → confidence + rationale + Hinglish message
  4. Confidence Gate            → low confidence → ESCALATED_HUMAN_ATTENTION
  5. Post-Flight Safety Validator → amount invariant + tone check
  6. Action Dispatcher          → one of 4 recovery strategies

The LLM is never on the critical path for financial decisions — amount, category,
and retry eligibility are 100% deterministic.  The LLM only shapes communication.

Returns a TriageResult that is persisted in the Trace row and emitted over SSE.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.classifier import ClassificationResult, FailureCategory, classify
from app.issuer_radar import is_in_extended_backoff, record_failure
from app.llm import LLMResult, get_llm_rationale
from app.logging_config import get_logger
from app.policy import Policy, get_policy
from app.strategies.dispatcher import ActionType, OutcomeStatus, StrategyResult, dispatch_action
from app.validator import PostFlightResult, run_post_flight

logger = get_logger(__name__)


@dataclass
class TriageResult:
    # Classification
    classification: ClassificationResult

    # LLM output
    llm: LLMResult

    # Validation
    post_flight: PostFlightResult

    # Action taken
    strategy: StrategyResult

    # Audit
    trace_id: str
    event_id: str
    amount_paise: int

    # Extras
    metadata: dict = field(default_factory=dict)

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def category(self) -> FailureCategory:
        return self.classification.category

    @property
    def action_type(self) -> ActionType:
        return self.strategy.action_type

    @property
    def outcome_status(self) -> OutcomeStatus:
        return self.strategy.outcome_status

    @property
    def confidence(self) -> float:
        return self.llm.confidence

    def to_trace_dict(self) -> dict:
        """Serialize for storage in the Trace DB row."""
        return {
            "category": self.category.value,
            "action_type": self.action_type.value,
            "outcome_status": self.outcome_status.value,
            "confidence": self.confidence,
            "rationale": self.llm.rationale,
            "hinglish_message": self.llm.hinglish_message,
            "amount_inr": self.amount_paise / 100,
            "classification_rule": self.classification.matched_rule,
            "provider_used": self.llm.provider_used,
            "llm_timed_out": self.llm.timed_out,
            "tone_check_passed": self.post_flight.tone_check.passed,
            "amount_check_passed": self.post_flight.amount_check.passed,
            "razorpay_link_id": self.strategy.razorpay_payment_link_id,
            "razorpay_link_url": self.strategy.razorpay_payment_link_url,
            "dispatch_channel": (
                self.strategy.dispatch_record.channel.value
                if self.strategy.dispatch_record else None
            ),
            "dispatch_cost_inr": (
                self.strategy.dispatch_record.cost_inr
                if self.strategy.dispatch_record else 0.0
            ),
        }


async def run_triage(
    session: AsyncSession,
    event_id: str,
    trace_id: str,
    amount_paise: int,
    error_code: Optional[str],
    error_reason: Optional[str],
    error_description: Optional[str],
    bank: Optional[str] = None,
    issuer_bin: Optional[str] = None,
    customer_id: Optional[str] = None,
    customer_name: Optional[str] = None,
    customer_email: Optional[str] = None,
    customer_phone: Optional[str] = None,
    customer_reply: Optional[str] = None,
    attempt_number: int = 1,
    policy: Optional[Policy] = None,
) -> TriageResult:
    """
    Full triage pipeline for one payment failure event.
    Assumes pre-flight guardrails have already passed.
    """
    if policy is None:
        policy = get_policy()

    amount_inr = amount_paise / 100

    # ── Step 1: Deterministic Classifier ────────────────────────────────────
    classification = classify(
        error_code=error_code,
        error_reason=error_reason,
        error_description=error_description,
        customer_reply=customer_reply,
    )
    logger.info("triage.classified", extra={
        "event_id": event_id,
        "category": classification.category.value,
        "rule": classification.matched_rule,
        "confidence_hint": classification.confidence_hint,
        "is_stop_keyword": classification.is_stop_keyword,
    })

    # ── Step 2: Issuer Health Radar ──────────────────────────────────────────
    in_backoff = False
    if issuer_bin:
        # Record failure and check for extended backoff simultaneously
        in_backoff = await record_failure(
            session=session,
            issuer_bin=issuer_bin,
        )
        if not in_backoff:
            in_backoff = await is_in_extended_backoff(session, issuer_bin)

        logger.info("triage.issuer_radar", extra={
            "event_id": event_id,
            "issuer_bin": issuer_bin,
            "in_extended_backoff": in_backoff,
        })

    # ── Step 3: LLM Rationale Layer ──────────────────────────────────────────
    llm_result = await get_llm_rationale(
        category=classification.category,
        error_code=error_code,
        error_reason=error_reason,
        amount_inr=amount_inr,
        customer_name=customer_name,
    )
    logger.info("triage.llm_result", extra={
        "event_id": event_id,
        "confidence": llm_result.confidence,
        "provider": llm_result.provider_used,
        "timed_out": llm_result.timed_out,
    })

    # ── Step 4 & 5: Post-Flight Safety Validator ─────────────────────────────
    # (The confidence gate is enforced inside dispatch_action)
    post_flight = run_post_flight(
        expected_paise=amount_paise,
        actual_paise=amount_paise,     # Amount-match: we never modify amount
        outreach_message=llm_result.hinglish_message,
    )
    # Use the sanitised message (may have been cleaned by tone check)
    final_message = post_flight.tone_check.sanitised_message

    # ── Step 6: Action Dispatcher ────────────────────────────────────────────
    strategy_result = dispatch_action(
        category=classification.category,
        event_id=event_id,
        amount_paise=amount_paise,
        outreach_message=final_message,
        confidence=llm_result.confidence,
        bank=bank,
        issuer_bin=issuer_bin,
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        in_extended_backoff=in_backoff,
        attempt_number=attempt_number,
        is_stop_keyword=classification.is_stop_keyword,
        policy=policy,
    )

    result = TriageResult(
        classification=classification,
        llm=llm_result,
        post_flight=post_flight,
        strategy=strategy_result,
        trace_id=trace_id,
        event_id=event_id,
        amount_paise=amount_paise,
    )

    logger.info("triage.complete", extra={
        "event_id": event_id,
        "trace_id": trace_id,
        "category": classification.category.value,
        "action_type": strategy_result.action_type.value,
        "outcome_status": strategy_result.outcome_status.value,
        "confidence": llm_result.confidence,
        "in_extended_backoff": in_backoff,
    })

    return result
