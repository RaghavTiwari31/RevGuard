"""
RevGuard — Synthetic Dataset Generator

Generates a 100-record benchmark dataset using Faker that exactly matches
the six benchmark category distributions and expected yields from the spec.

Category distribution (100 records, ₹5,40,000 total):
  CAT_01  TRANSIENT_DOWNTIME    20 records  ₹ 60,000  (avg ₹3,000)
  CAT_02  TEMPORARY_CASHFLOW    40 records  ₹3,20,000  (avg ₹8,000)
  CAT_03  EXPIRED_MANDATE       20 records  ₹1,00,000  (avg ₹5,000)
  CAT_04  LOW_CONFIDENCE         5 records  ₹ 20,000   (avg ₹4,000)
  CAT_05  FRAUD_OR_DISPUTE      10 records  ₹ 20,000   (avg ₹2,000)
  CAT_06  MAX_RETRIES            5 records  ₹ 20,000   (avg ₹4,000)
  ─────────────────────────────────────────────────────────────────
  TOTAL                        100 records  ₹5,40,000

Expected yield with RevGuard:
  CAT_01: 75% = ₹45,000
  CAT_02: 85% = ₹2,72,000
  CAT_03: 70% = ₹70,000
  CAT_04: 30% = ₹6,000  (human escalation)
  CAT_05:  0% = ₹0       (circuit breaker)
  CAT_06:  0% = ₹0       (circuit breaker)
  ────────────────────────── Target: ₹3,93,000 (72.8% > 65%)

Each record is a dict mimicking the Razorpay webhook payload format so
the /simulate endpoint can replay them through the exact webhook code path.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from typing import Optional

from faker import Faker

fake = Faker("en_IN")
random.seed(42)  # Reproducible dataset across runs

# ── Category specs ────────────────────────────────────────────────────────────

_CAT_SPECS = [
    # (category_tag, count, avg_amount_inr, error_codes, error_reasons)
    (
        "CAT_01_TRANSIENT",
        20,
        3000,
        ["gateway_timeout", "bank_technical_error", "bank_down", "server_error", "network_error"],
        ["payment_timeout", "bank_connection_error", "acquirer_timeout"],
    ),
    (
        "CAT_02_CASHFLOW",
        40,
        8000,
        ["insufficient_funds", "do_not_honor", "credit_limit_exceeded", "not_sufficient_funds"],
        ["insufficient_balance", "card_limit_exceeded", "insufficient_funds"],
    ),
    (
        "CAT_03_MANDATE",
        20,
        5000,
        ["mandate_expired", "mandate_revoked", "token_expired", "subscription_cancelled"],
        ["mandate_expired", "nach_debit_failed", "recurring_token_invalid"],
    ),
    (
        "CAT_04_LOWCONF",
        5,
        4000,
        # Generic codes that produce low classifier confidence
        ["BAD_REQUEST_ERROR", "GATEWAY_ERROR", "unknown_error"],
        ["payment_failed", "unknown_failure"],
    ),
    (
        "CAT_05_FRAUD",
        10,
        2000,
        ["fraud", "risk_threshold", "suspected_fraud", "velocity_rule"],
        ["fraud_detected", "risk_threshold_exceeded"],
    ),
    (
        "CAT_06_MAXRETRY",
        5,
        4000,
        ["insufficient_funds", "do_not_honor"],
        ["insufficient_balance"],
    ),
]

# Indian bank BINs (first 6 digits approximations for testing)
_BINS = ["411111", "414720", "424242", "437280", "512345", "524321", "601200", "652100"]
_BANKS = ["HDFC Bank", "ICICI Bank", "SBI", "Axis Bank", "Kotak Bank", "Yes Bank"]


def _make_amount_paise(avg_inr: int, jitter_pct: float = 0.3) -> int:
    """Generate a realistic amount with ±jitter% variation, rounded to ₹10."""
    delta = int(avg_inr * jitter_pct * (random.random() * 2 - 1))
    amount_inr = max(100, avg_inr + delta)
    # Round to nearest ₹10
    amount_inr = round(amount_inr / 10) * 10
    return amount_inr * 100   # Convert to paise


def _make_payment_entity(
    cat_tag: str,
    error_code: str,
    error_reason: str,
    amount_paise: int,
    attempt_number: int = 1,
) -> dict:
    payment_id = f"pay_{uuid.uuid4().hex[:16]}"
    customer_id = f"cust_{fake.ean(length=8)}"
    return {
        "id": payment_id,
        "amount": amount_paise,
        "currency": "INR",
        "status": "failed",
        "order_id": f"order_{uuid.uuid4().hex[:12]}",
        "description": f"RevGuard Test — {cat_tag}",
        "email": fake.ascii_email(),
        "contact": f"+91{fake.numerify('##########')}",
        "customer_id": customer_id,
        "error_code": error_code,
        "error_description": f"Payment failed: {error_reason.replace('_', ' ')}",
        "error_reason": error_reason,
        "error_source": "bank",
        "error_step": "payment_authorization",
        "bank": random.choice(_BANKS),
        "card_id": f"{random.choice(_BINS)}{fake.numerify('##########')}",
        # Metadata for the batch runner
        "_cat_tag": cat_tag,
        "_attempt_number": attempt_number,
    }


def generate_dataset(
    seed: int = 42,
    num_records: int = 100,
) -> list[dict]:
    """
    Generate the 100-record benchmark dataset.
    Returns a list of Razorpay webhook payload dicts.
    Each record goes through the exact same code path as a live webhook.
    """
    random.seed(seed)
    Faker.seed(seed)

    records: list[dict] = []

    for cat_tag, count, avg_inr, codes, reasons in _CAT_SPECS:
        for i in range(count):
            error_code = random.choice(codes)
            error_reason = random.choice(reasons)
            amount_paise = _make_amount_paise(avg_inr)

            # CAT_06: simulate 3 prior attempts so retry cap is hit
            attempt_number = 4 if cat_tag == "CAT_06_MAXRETRY" else 1

            payment = _make_payment_entity(
                cat_tag=cat_tag,
                error_code=error_code,
                error_reason=error_reason,
                amount_paise=amount_paise,
                attempt_number=attempt_number,
            )

            # Wrap in Razorpay webhook envelope
            record = {
                "entity": "event",
                "event": "payment.failed",
                "contains": ["payment"],
                "payload": {"payment": {"entity": payment}},
                "created_at": int(datetime.now(timezone.utc).timestamp()),
                "_meta": {
                    "cat_tag": cat_tag,
                    "attempt_number": attempt_number,
                    "expected_category": _cat_to_expected_category(cat_tag),
                },
            }
            records.append(record)

    # Shuffle so categories are interspersed (realistic)
    random.shuffle(records)
    return records


def _cat_to_expected_category(cat_tag: str) -> str:
    mapping = {
        "CAT_01_TRANSIENT": "TRANSIENT_DOWNTIME",
        "CAT_02_CASHFLOW": "TEMPORARY_CASHFLOW",
        "CAT_03_MANDATE": "EXPIRED_MANDATE",
        "CAT_04_LOWCONF": "ESCALATED_HUMAN_ATTENTION",
        "CAT_05_FRAUD": "CIRCUIT_BREAKER",
        "CAT_06_MAXRETRY": "CIRCUIT_BREAKER",
    }
    return mapping.get(cat_tag, "UNKNOWN")


def dataset_summary() -> dict:
    """Return the expected batch totals for validation."""
    total_count = sum(c for _, c, _, _, _ in _CAT_SPECS)
    total_amount = sum(c * a * 100 for _, c, a, _, _ in _CAT_SPECS)  # rough, in paise
    return {
        "total_records": total_count,
        "total_amount_inr_approx": total_amount / 100,
        "category_counts": {
            cat: count for cat, count, _, _, _ in _CAT_SPECS
        },
    }
