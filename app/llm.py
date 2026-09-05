"""
RevGuard — LLM Rationale Layer

Generates:
  1. A confidence score (0–1) for the triage category
  2. A human-readable rationale (English, 1–2 sentences)
  3. A Hinglish customer outreach message (compliant with tone policy)

Design rules (from the implementation plan):
  - The LLM NEVER decides the failure category — it receives the category
    from the deterministic classifier and generates explanation/copy only.
  - If the LLM call fails or times out, we fall back to a canned template
    for the category — the pipeline MUST NOT 500 due to an LLM failure.
  - Provider is pluggable via the LLM_PROVIDER env var (groq | gemini).
  - A token-bucket throttle is applied by the batch runner (Phase 3); here
    we only add a per-call timeout.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from app.classifier import FailureCategory
from app.logging_config import get_logger

logger = get_logger(__name__)

# ── Per-call timeout (seconds) ────────────────────────────────────────────────
LLM_TIMEOUT_SECONDS = 8.0

# ── Canned fallback templates ─────────────────────────────────────────────────
# One per category — used when the LLM is unavailable.
_CANNED: dict[FailureCategory, dict] = {
    FailureCategory.TRANSIENT_DOWNTIME: {
        "confidence": 0.80,
        "rationale": (
            "Payment failed due to a temporary bank or gateway issue. "
            "A retry has been scheduled during optimal banking hours."
        ),
        "hinglish_message": (
            "Namaste! Aapka payment temporarily fail hua hai bank ki technical "
            "problem ki wajah se. Hum jald hi retry karenge — koi action nahi chahiye. "
            "Shukriya! 🙏"
        ),
    },
    FailureCategory.TEMPORARY_CASHFLOW: {
        "confidence": 0.85,
        "rationale": (
            "Payment declined due to insufficient funds in the customer's account. "
            "A payment link has been generated for when funds are available."
        ),
        "hinglish_message": (
            "Namaste! Aapka payment insufficient funds ki wajah se complete nahi "
            "hua. Neeche diye gaye link se convenient time par payment kar sakte hain. "
            "Koi bhi help chahiye toh batayein! 😊"
        ),
    },
    FailureCategory.EXPIRED_MANDATE: {
        "confidence": 0.85,
        "rationale": (
            "The recurring payment mandate has expired or been revoked. "
            "The customer needs to re-authorise the mandate to resume payments."
        ),
        "hinglish_message": (
            "Namaste! Aapka recurring payment mandate expire ho gaya hai. "
            "Payments continue karne ke liye please mandate dobara register karein — "
            "link neeche hai. Dhanyawad! 🙏"
        ),
    },
    FailureCategory.DISPUTE_OR_OPTOUT: {
        "confidence": 0.95,
        "rationale": (
            "Customer has flagged this transaction as unauthorised or requested "
            "to stop communications. All automation has been frozen per policy."
        ),
        "hinglish_message": (
            "Aapki request note kar li gayi hai. Hamare team ka ek member "
            "jald hi aapse directly contact karega. Shukriya! 🙏"
        ),
    },
    FailureCategory.UNRECOVERABLE_FRAUD: {
        "confidence": 0.95,
        "rationale": (
            "Transaction flagged by the issuer's fraud detection system. "
            "No automated recovery action will be taken for security reasons."
        ),
        "hinglish_message": (
            "Aapki account security ke liye yeh transaction block ki gayi hai. "
            "Kisi bhi samasya ke liye hamare support se contact karein. 🙏"
        ),
    },
}


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class LLMResult:
    confidence: float
    rationale: str
    hinglish_message: str
    provider_used: str          # "groq" | "gemini" | "canned_fallback"
    timed_out: bool = False


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(
    category: FailureCategory,
    error_code: Optional[str],
    error_reason: Optional[str],
    amount_inr: float,
    customer_name: Optional[str],
) -> str:
    name = customer_name or "the customer"
    return f"""You are RevGuard, a revenue recovery AI for an Indian fintech company.

A payment has failed and been classified as: {category.value}
Error code: {error_code or "unknown"}
Error reason: {error_reason or "unknown"}
Amount: ₹{amount_inr:.2f}
Customer name: {name}

Your task (respond in JSON only, no extra text):
{{
  "confidence": <float 0.0–1.0 representing how confident you are in this classification>,
  "rationale": "<1-2 sentence English explanation of why this failure occurred and what action is being taken>",
  "hinglish_message": "<friendly Hinglish WhatsApp/SMS message to {name} about this payment failure, max 160 chars, no threats, no guarantees, no coercive language>"
}}

Rules:
- confidence must reflect classification certainty, NOT recovery probability
- hinglish_message must be polite, helpful, and TRAI-compliant (no threats, no urgency pressure)
- If the category is DISPUTE_OR_OPTOUT or UNRECOVERABLE_FRAUD, hinglish_message must NOT ask for payment
- Respond ONLY with the JSON object, no markdown fences
"""


# ── LLM call implementations ──────────────────────────────────────────────────

async def _call_groq(prompt: str) -> dict:
    """Call Groq's API (Llama 3.3 70B or 3.1 8B) for fast, free inference."""
    import groq as groq_sdk

    client = groq_sdk.AsyncGroq(api_key=os.getenv("GROQ_API_KEY", ""))
    response = await client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    import json
    return json.loads(response.choices[0].message.content)


async def _call_gemini(prompt: str) -> dict:
    """Call Google Gemini (gemini-2.5-flash) using the new google-genai SDK."""
    import json
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=300,
            response_mime_type="application/json",
            # Disable thinking mode so we get clean JSON directly
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    # Safely extract text — skip "thought" parts (internal reasoning)
    text = response.text
    if text is None:
        for candidate in (response.candidates or []):
            for part in (getattr(candidate.content, "parts", None) or []):
                # thought=True means it's internal reasoning, not the answer
                if getattr(part, "thought", False):
                    continue
                if getattr(part, "text", None):
                    text = part.text
                    break
            if text:
                break
    if not text:
        raise ValueError("Gemini returned an empty response")
    return json.loads(text)


async def _llm_call_with_timeout(
    provider: str,
    prompt: str,
) -> dict:
    """Dispatch to the configured provider with a hard timeout."""
    if provider == "groq":
        coro = _call_groq(prompt)
    elif provider == "gemini":
        coro = _call_gemini(prompt)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider!r}")

    return await asyncio.wait_for(coro, timeout=LLM_TIMEOUT_SECONDS)


# ── Public entry point ────────────────────────────────────────────────────────

async def get_llm_rationale(
    category: FailureCategory,
    error_code: Optional[str] = None,
    error_reason: Optional[str] = None,
    amount_inr: float = 0.0,
    customer_name: Optional[str] = None,
) -> LLMResult:
    """
    Call the LLM to generate confidence + rationale + Hinglish message.

    Always returns a valid LLMResult — falls back to canned templates if the
    LLM is unavailable, timed out, or returns invalid JSON.  The pipeline
    MUST NOT block or 500 due to LLM unavailability.
    """
    provider = os.getenv("LLM_PROVIDER", "groq").lower()
    canned = _CANNED[category]

    # If no API key configured, skip the LLM call entirely (dev mode)
    api_key_env = "GROQ_API_KEY" if provider == "groq" else "GEMINI_API_KEY"
    if not os.getenv(api_key_env, "").strip():
        logger.info("llm.skipped_no_api_key", extra={
            "provider": provider,
            "category": category.value,
        })
        return LLMResult(
            confidence=canned["confidence"],
            rationale=canned["rationale"],
            hinglish_message=canned["hinglish_message"],
            provider_used="canned_fallback",
        )

    prompt = _build_prompt(category, error_code, error_reason, amount_inr, customer_name)

    try:
        data = await _llm_call_with_timeout(provider, prompt)

        # Validate the response shape
        confidence = float(data.get("confidence", canned["confidence"]))
        confidence = max(0.0, min(1.0, confidence))  # Clamp to [0, 1]

        result = LLMResult(
            confidence=confidence,
            rationale=str(data.get("rationale", canned["rationale"]))[:512],
            hinglish_message=str(data.get("hinglish_message", canned["hinglish_message"]))[:200],
            provider_used=provider,
        )
        logger.info("llm.success", extra={
            "provider": provider,
            "category": category.value,
            "confidence": confidence,
        })
        return result

    except asyncio.TimeoutError:
        logger.warning("llm.timeout", extra={
            "provider": provider,
            "timeout_seconds": LLM_TIMEOUT_SECONDS,
            "category": category.value,
            "fallback": "canned_template",
        })
        return LLMResult(
            confidence=canned["confidence"],
            rationale=canned["rationale"],
            hinglish_message=canned["hinglish_message"],
            provider_used="canned_fallback",
            timed_out=True,
        )

    except Exception as exc:
        logger.error("llm.error", extra={
            "provider": provider,
            "category": category.value,
            "error": str(exc),
            "fallback": "canned_template",
        })
        return LLMResult(
            confidence=canned["confidence"],
            rationale=canned["rationale"],
            hinglish_message=canned["hinglish_message"],
            provider_used="canned_fallback",
        )
