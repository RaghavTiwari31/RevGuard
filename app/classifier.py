"""
RevGuard — Deterministic Error-Code Classifier

Maps Razorpay error_code / error_reason / error_description → one of five
failure categories, in O(1).  The LLM is NEVER involved in category decisions
— this is the deterministic spine of the triage engine.

Categories (from the original spec):
  TRANSIENT_DOWNTIME      — Gateway timeout, bank technical error; retry later
  TEMPORARY_CASHFLOW      — Insufficient funds; generate a payment link
  EXPIRED_MANDATE         — Mandate expired/revoked; re-register
  DISPUTE_OR_OPTOUT       — Stop-keyword reply / dispute signal; freeze automation
  UNRECOVERABLE_FRAUD     — Known fraud markers; abandon, log only

Classification priority (highest → lowest):
  1. Stop-keyword / dispute signals    → DISPUTE_OR_OPTOUT
  2. Known fraud markers               → UNRECOVERABLE_FRAUD
  3. Mandate / subscription codes      → EXPIRED_MANDATE
  4. Insufficient-funds codes          → TEMPORARY_CASHFLOW
  5. Gateway / bank technical errors   → TRANSIENT_DOWNTIME
  6. Default (unknown error)           → TRANSIENT_DOWNTIME (safe, retryable)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ── Category enum ─────────────────────────────────────────────────────────────

class FailureCategory(str, Enum):
    TRANSIENT_DOWNTIME = "TRANSIENT_DOWNTIME"
    TEMPORARY_CASHFLOW = "TEMPORARY_CASHFLOW"
    EXPIRED_MANDATE = "EXPIRED_MANDATE"
    DISPUTE_OR_OPTOUT = "DISPUTE_OR_OPTOUT"
    UNRECOVERABLE_FRAUD = "UNRECOVERABLE_FRAUD"


# ── Lookup tables (all lowercase for case-insensitive matching) ───────────────

# Razorpay error_code values
_FRAUD_CODES: frozenset[str] = frozenset({
    "fraud",
    "risk_threshold",
    "issuer_fraud_risk",
    "suspected_fraud",
    "fraud_suspected",
    "velocity_rule",
    "fraud_rule",
    "blacklisted_card",
    "card_blacklisted",
})

_MANDATE_CODES: frozenset[str] = frozenset({
    "mandate_expired",
    "mandate_revoked",
    "mandate_cancelled",
    "mandate_invalid",
    "emandate_revoked",
    "nach_debit_failed_mandate_expired",
    "nach_debit_failed_mandate_revoked",
    "nach_debit_failed_mandate_cancelled",
    "recurring_charge_failed_mandate_expired",
    "subscription_cancelled",
    "subscription_completed",
    "token_expired",
    "invalid_recurring_token",
})

_CASHFLOW_CODES: frozenset[str] = frozenset({
    "insufficient_funds",
    "insufficient_balance",
    "do_not_honor",
    "do_not_try_again",
    "not_sufficient_funds",
    "no_credit_account",
    "no_such_account",
    "exceeds_withdrawal_amount_limit",
    "exceeds_withdrawal_frequency_limit",
    "credit_limit_exceeded",
    "card_limit_exceeded",
    "amount_limit_exceeded",
    "transaction_not_permitted_to_cardholder",
    "transaction_limit_exceeded",
})

_TRANSIENT_CODES: frozenset[str] = frozenset({
    "gateway_timeout",
    "bank_technical_error",
    "bank_down",
    "acquirer_down",
    "issuer_down",
    "technical_error",
    "server_error",
    "timeout",
    "connection_error",
    "network_error",
    "service_unavailable",
    "bad_gateway",
    "gateway_error",
    "payment_timeout",
    "payment_processing_failed",
    "internal_server_error",
    "GATEWAY_ERROR",
    "BAD_REQUEST_ERROR",   # generic Razorpay wrapper — treat as transient default
})

# Razorpay error_reason patterns (substring match)
_MANDATE_REASON_PATTERNS: list[str] = [
    "mandate", "nach", "recurring", "emandate", "subscription",
    "token_expired", "recurring_token",
]

_CASHFLOW_REASON_PATTERNS: list[str] = [
    "insufficient", "funds", "balance", "credit_limit", "do_not_honor",
    "limit_exceeded",
]

_FRAUD_REASON_PATTERNS: list[str] = [
    "fraud", "risk", "blacklist", "velocity",
]

# Stop/dispute keywords in free-text fields (case-insensitive)
_STOP_KEYWORDS: list[str] = [
    "unauthorized", "fraud", "unsubscribe", "stop", "dispute",
    "chargeback", "not my transaction", "not authorised",
    "didn't do this", "i didn't", "i did not", "cancel subscription",
    "refund", "cancel my", "opt out",
]
_STOP_PATTERN = re.compile(
    "|".join(re.escape(k) for k in _STOP_KEYWORDS),
    re.IGNORECASE,
)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class ClassificationResult:
    category: FailureCategory
    matched_rule: str               # human-readable rule name for audit trail
    confidence_hint: float          # 1.0 = exact code match, 0.7 = reason pattern, 0.5 = default
    is_stop_keyword: bool = False


# ── Core classifier ───────────────────────────────────────────────────────────

def classify(
    error_code: Optional[str],
    error_reason: Optional[str],
    error_description: Optional[str],
    customer_reply: Optional[str] = None,
) -> ClassificationResult:
    """
    Deterministic O(1) classifier.  Call this BEFORE the LLM — the LLM only
    receives the category for rationale generation, never decides it.

    Parameters
    ----------
    error_code        : Razorpay error_code field (e.g. "insufficient_funds")
    error_reason      : Razorpay error_reason field (e.g. "payment_failed")
    error_description : Free-text description from Razorpay
    customer_reply    : Inbound customer SMS/WhatsApp reply (if any)
    """
    code = (error_code or "").lower().strip()
    reason = (error_reason or "").lower().strip()
    desc = (error_description or "").lower().strip()
    reply = (customer_reply or "").lower().strip()

    # ── Priority 1: Stop-keyword / dispute signal ─────────────────────────────
    # Only check free-text fields (description + customer reply) — NOT machine
    # error codes, which can legitimately contain words like "fraud".
    human_text = f"{desc} {reply}"
    if _STOP_PATTERN.search(human_text):
        return ClassificationResult(
            category=FailureCategory.DISPUTE_OR_OPTOUT,
            matched_rule="stop_keyword_pattern",
            confidence_hint=1.0,
            is_stop_keyword=True,
        )

    # ── Priority 2: Fraud markers ─────────────────────────────────────────────
    if code in _FRAUD_CODES:
        return ClassificationResult(
            category=FailureCategory.UNRECOVERABLE_FRAUD,
            matched_rule=f"fraud_code_exact:{code}",
            confidence_hint=1.0,
        )
    if any(p in code or p in reason for p in _FRAUD_REASON_PATTERNS if p not in ("fraud",)):
        return ClassificationResult(
            category=FailureCategory.UNRECOVERABLE_FRAUD,
            matched_rule="fraud_reason_pattern",
            confidence_hint=0.85,
        )

    # ── Priority 3: Mandate / subscription codes ──────────────────────────────
    if code in _MANDATE_CODES:
        return ClassificationResult(
            category=FailureCategory.EXPIRED_MANDATE,
            matched_rule=f"mandate_code_exact:{code}",
            confidence_hint=1.0,
        )
    if any(p in code or p in reason for p in _MANDATE_REASON_PATTERNS):
        return ClassificationResult(
            category=FailureCategory.EXPIRED_MANDATE,
            matched_rule="mandate_reason_pattern",
            confidence_hint=0.75,
        )

    # ── Priority 4: Insufficient funds ───────────────────────────────────────
    if code in _CASHFLOW_CODES:
        return ClassificationResult(
            category=FailureCategory.TEMPORARY_CASHFLOW,
            matched_rule=f"cashflow_code_exact:{code}",
            confidence_hint=1.0,
        )
    if any(p in code or p in reason for p in _CASHFLOW_REASON_PATTERNS):
        return ClassificationResult(
            category=FailureCategory.TEMPORARY_CASHFLOW,
            matched_rule="cashflow_reason_pattern",
            confidence_hint=0.75,
        )

    # ── Priority 5: Gateway / bank technical errors ───────────────────────────
    if code in _TRANSIENT_CODES:
        return ClassificationResult(
            category=FailureCategory.TRANSIENT_DOWNTIME,
            matched_rule=f"transient_code_exact:{code}",
            confidence_hint=0.95,
        )

    # ── Priority 6: Default — unknown error, treat as transient (safe retry) ──
    return ClassificationResult(
        category=FailureCategory.TRANSIENT_DOWNTIME,
        matched_rule="default_fallback",
        confidence_hint=0.5,
    )
