"""
RevGuard — Post-Flight Safety Validator

Two deterministic checks run AFTER the LLM and BEFORE any Razorpay API call:

  1. Amount-Match Invariant — the amount in the generated action must exactly
     match the original `amount_due` from the webhook.  Hard 422 abort if
     mismatch — no Razorpay call is made.

  2. Tone Check — static regex/keyword blocklist over the LLM-generated
     outreach message.  Coercive, threatening, or guarantee-language is
     rejected and the message is replaced with the canned fallback.
     Pure Python, sub-millisecond, fully auditable.

These are deterministic boundaries that judges can test live.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.logging_config import get_logger

logger = get_logger(__name__)

# ── Blocked tone patterns ─────────────────────────────────────────────────────
# Any message containing these patterns is rejected.

_BLOCKED_PATTERNS: list[tuple[str, str]] = [
    # Threats
    (r"\blegal action\b", "threat:legal_action"),
    (r"\bsue\b|\bsuing\b|\blawsuit\b", "threat:lawsuit"),
    (r"\bcriminal\b", "threat:criminal"),
    (r"\bpolice\b|\bcourt\b", "threat:law_enforcement"),
    (r"\bblacklist(ed|ing)?\b", "threat:blacklist"),
    (r"\bdefault\b.*\breport\b|\breport\b.*\bdefault\b", "threat:credit_report"),
    # Guarantees / false promises
    (r"\bguarantee\b", "guarantee"),
    (r"\bpromise\b.*\brefund\b|\brefund\b.*\bpromise\b", "false_refund_promise"),
    # Coercive urgency
    (r"\bimmediate\b.*\bpay\b|\bpay\b.*\bimmediately\b", "coercive_urgency"),
    (r"\bor else\b", "coercive_threat"),
    (r"\blast chance\b|\bfinal warning\b", "coercive_ultimatum"),
    # Sensitive opt-out instructions (must not appear in dunning messages)
    (r"\bunsubscribe\b|\bopt.?out\b", "optout_in_dunning"),
]

_COMPILED_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in _BLOCKED_PATTERNS
]

# Safe fallback message used when tone check fails
_SAFE_FALLBACK_MESSAGE = (
    "Namaste! Aapke payment ke baare mein ek important update hai. "
    "Please hamare customer care se contact karein. Dhanyawad! 🙏"
)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class AmountCheckResult:
    passed: bool
    expected_paise: int
    actual_paise: int
    reason: str


@dataclass
class ToneCheckResult:
    passed: bool
    blocked_patterns: list[str]
    sanitised_message: str      # Safe message to use (original if passed, fallback if blocked)


@dataclass
class PostFlightResult:
    amount_check: AmountCheckResult
    tone_check: ToneCheckResult
    passed: bool
    rejection_reason: str = ""


# ── Validators ────────────────────────────────────────────────────────────────

def check_amount(
    expected_paise: int,
    actual_paise: int,
) -> AmountCheckResult:
    """
    Hard invariant: the action amount must exactly equal the original event amount.
    A mismatch triggers an immediate 422 abort — no Razorpay API call is made.
    """
    passed = expected_paise == actual_paise
    result = AmountCheckResult(
        passed=passed,
        expected_paise=expected_paise,
        actual_paise=actual_paise,
        reason=(
            "Amount matches"
            if passed
            else (
                f"Amount mismatch: expected ₹{expected_paise/100:.2f} "
                f"but action has ₹{actual_paise/100:.2f} — aborting"
            )
        ),
    )
    logger.info("validator.amount_check", extra={
        "passed": passed,
        "expected_paise": expected_paise,
        "actual_paise": actual_paise,
        "reason": result.reason,
    })
    return result


def check_tone(message: str) -> ToneCheckResult:
    """
    Static regex tone check over the LLM-generated outreach message.
    If any blocked pattern matches, the message is replaced with the safe fallback.
    This check is pure Python — sub-millisecond, no network call.
    """
    blocked: list[str] = []
    for pattern, label in _COMPILED_PATTERNS:
        if pattern.search(message):
            blocked.append(label)

    passed = len(blocked) == 0
    sanitised = message if passed else _SAFE_FALLBACK_MESSAGE

    result = ToneCheckResult(
        passed=passed,
        blocked_patterns=blocked,
        sanitised_message=sanitised,
    )
    logger.info("validator.tone_check", extra={
        "passed": passed,
        "blocked_patterns": blocked,
        "message_replaced": not passed,
    })
    return result


def run_post_flight(
    expected_paise: int,
    actual_paise: int,
    outreach_message: str,
) -> PostFlightResult:
    """
    Run both post-flight checks.  Amount mismatch is a hard failure (the caller
    should return HTTP 422).  Tone failure sanitises the message but does not
    block the pipeline.
    """
    amount = check_amount(expected_paise, actual_paise)
    tone = check_tone(outreach_message)

    passed = amount.passed  # Tone failure sanitises message but doesn't block
    rejection_reason = amount.reason if not amount.passed else ""

    logger.info("postflight.result", extra={
        "amount_passed": amount.passed,
        "tone_passed": tone.passed,
        "overall_passed": passed,
        "rejection_reason": rejection_reason,
    })

    return PostFlightResult(
        amount_check=amount,
        tone_check=tone,
        passed=passed,
        rejection_reason=rejection_reason,
    )
