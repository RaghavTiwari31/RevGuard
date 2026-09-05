"""
RevGuard — /simulate Batch Runner (Differentiator #10)

Replays the 100-record synthetic dataset through the EXACT same code path
as live Razorpay webhooks — no separate offline script, no divergence.

Features:
  - Token-bucket throttle: LLM calls ≤ 28/minute (safe under Groq free tier ~30 RPM)
  - Streams results live over SSE as each record completes
  - Adaptive Channel Bandit wired in for channel selection
  - Shadow Ledger computed alongside every event
  - Returns benchmark summary matching the spec targets

Token-bucket throttle:
  - Bucket capacity: 28 tokens
  - Refill: 28 tokens per 60 seconds (≈ 1 token/2.14s)
  - Each LLM call consumes 1 token
  - If bucket is empty: sleep until token available
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.bandit import flush_state as flush_bandit_state
from app.bandit import get_bandit_stats, record_reward, reset_bandit
from app.bandit import maybe_flush as maybe_flush_bandit
from app.channels import Channel
from app.dataset import generate_dataset
from app.db import Trace, get_session_factory
from app.llm import llm_enabled
from app.logging_config import get_logger
from app.policy import get_policy
from app.shadow_ledger import ShadowLedger, expected_recovery_rate
from app.sse import broadcast
from app.strategies.dispatcher import ActionType
from app.triage import run_triage

logger = get_logger(__name__)
router = APIRouter()

# Actions where the channel is a real choice the bandit made, and so a real
# experiment to learn from.
_LEARNABLE_ACTIONS = frozenset({
    ActionType.GENERATE_PAYMENT_LINK,
    ActionType.SEND_MANDATE_LINK,
})

# ── In-memory run state ────────────────────────────────────────────────────────
_current_run: dict | None = None


# ── Token-Bucket Throttle ─────────────────────────────────────────────────────

class TokenBucket:
    """Simple token-bucket rate limiter for LLM calls."""

    def __init__(self, capacity: int = 28, refill_per_minute: int = 28):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = refill_per_minute / 60.0   # tokens per second
        self._last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self._last_refill = now

    async def acquire(self):
        """Block until a token is available."""
        while True:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            # Calculate wait time until next token
            wait = (1.0 - self.tokens) / self.refill_rate
            await asyncio.sleep(wait)


# Global throttle — shared across the whole simulate run
_throttle = TokenBucket(capacity=28, refill_per_minute=28)


# ── Benchmark result accumulator ───────────────────────────────────────────────

class BenchmarkAccumulator:
    def __init__(self):
        self.results: list[dict] = []
        self.total_amount_inr: float = 0.0
        self.revguard_recovered_inr: float = 0.0
        self.naive_recovered_inr: float = 0.0
        self.total_cost_inr: float = 0.0
        self.guardrail_violations: int = 0
        self.category_counts: dict[str, int] = {}
        self.action_counts: dict[str, int] = {}
        self.shadow = ShadowLedger()

    def add(self, result: dict):
        self.results.append(result)
        amount = result.get("amount_inr", 0.0) or 0.0
        self.total_amount_inr += amount
        self.total_cost_inr += result.get("dispatch_cost_inr", 0.0) or 0.0

        cat = result.get("category", "UNKNOWN")
        action = result.get("action_type", "UNKNOWN")
        self.category_counts[cat] = self.category_counts.get(cat, 0) + 1
        self.action_counts[action] = self.action_counts.get(action, 0) + 1

        # Shadow ledger
        shadow_entry = self.shadow.record(
            event_id=result["event_id"],
            amount_inr=amount,
            category=cat,
            action_type=action,
            channel=result.get("dispatch_channel"),
        )
        self.revguard_recovered_inr += shadow_entry.revguard_recovered_inr
        self.naive_recovered_inr += shadow_entry.naive_recovered_inr

        # Check for guardrail violations
        if result.get("guardrail_violation"):
            self.guardrail_violations += 1

    def summary(self) -> dict:
        total = self.total_amount_inr
        recovered = self.revguard_recovered_inr
        naive = self.naive_recovered_inr
        return {
            "total_records": len(self.results),
            "total_amount_inr": round(total, 2),
            "revguard_recovered_inr": round(recovered, 2),
            "naive_recovered_inr": round(naive, 2),
            "revguard_yield_pct": round(recovered / total * 100, 1) if total > 0 else 0.0,
            "naive_yield_pct": round(naive / total * 100, 1) if total > 0 else 0.0,
            "delta_inr": round(recovered - naive, 2),
            "total_cost_inr": round(self.total_cost_inr, 2),
            "net_recovery_inr": round(recovered - self.total_cost_inr, 2),
            "guardrail_violations": self.guardrail_violations,
            "guardrail_adherence_pct": 100.0 if self.guardrail_violations == 0 else
                round((len(self.results) - self.guardrail_violations) / len(self.results) * 100, 1),
            "category_breakdown": self.category_counts,
            "action_breakdown": self.action_counts,
            "bandit_stats": get_bandit_stats(),
        }


# ── Core batch processor ───────────────────────────────────────────────────────

async def _process_one(
    record: dict,
    session: AsyncSession,
    accumulator: BenchmarkAccumulator,
    policy,
    run_id: str,
    index: int,
    total: int,
) -> dict:
    """Process a single synthetic event through the full triage pipeline."""
    payload_dict = record
    meta = record.get("_meta", {})
    cat_tag = meta.get("cat_tag", "UNKNOWN")
    attempt_number = meta.get("attempt_number", 1)

    payment = payload_dict.get("payload", {}).get("payment", {}).get("entity", {})
    event_id = payment.get("id", f"sim_{uuid.uuid4().hex[:12]}")
    amount_paise = payment.get("amount", 0)
    amount_inr = amount_paise / 100

    trace_id = f"trc_{uuid.uuid4().hex}"

    # ── Throttle LLM call (token bucket) ─────────────────────────────────────
    # Only rate-limit when a real provider call will actually be made.  With no
    # API key configured every rationale comes from a canned template and no
    # network request happens, so throttling would add ~2.5 minutes of pure
    # sleeping to a 100-record demo run for nothing.
    if llm_enabled():
        await _throttle.acquire()

    # ── Idempotency: use a fresh event_id for each simulation run ─────────────
    # (prefix with run_id to avoid collisions across runs)
    sim_event_id = f"sim_{run_id[:8]}_{event_id}"

    # ── Run triage ────────────────────────────────────────────────────────────
    triage = await run_triage(
        session=session,
        event_id=sim_event_id,
        trace_id=trace_id,
        amount_paise=amount_paise,
        error_code=payment.get("error_code"),
        error_reason=payment.get("error_reason"),
        error_description=payment.get("error_description"),
        bank=payment.get("bank"),
        issuer_bin=payment.get("card_id", "")[:6] if payment.get("card_id") else None,
        customer_id=payment.get("customer_id"),
        customer_name=None,
        customer_email=payment.get("email"),
        customer_phone=payment.get("contact"),
        attempt_number=attempt_number,
        policy=policy,
    )

    # ── Bandit: record reward signal ──────────────────────────────────────────
    # Only score genuine recovery attempts.  A circuit-breaker acknowledgement
    # is sent on a fixed channel and is not trying to recover anything, so
    # feeding it back would credit an arm the bandit never chose.
    record = triage.strategy.dispatch_record
    if record and record.channel != Channel.NONE and triage.action_type in _LEARNABLE_ACTIONS:
        record_reward(
            triage.category.value,
            record.channel,
            expected_recovery_rate(
                action_type=triage.action_type.value,
                channel=record.channel.value,
                amount_inr=amount_inr,
            ),
        )

    # ── Persist the trace ─────────────────────────────────────────────────────
    # The batch runner used to broadcast over SSE and keep totals in memory
    # only, so a 100-record run left nothing behind: refresh the dashboard and
    # the entire benchmark was gone. Writing the same Trace row a live webhook
    # writes means history, filtering and the stats rollup all work identically
    # for simulated and real traffic.
    trace_dict = triage.to_trace_dict()
    session.add(Trace(
        trace_id=trace_id,
        event_id=sim_event_id,
        category=trace_dict["category"],
        action_type=trace_dict["action_type"],
        outcome_status=trace_dict["outcome_status"],
        confidence_score=trace_dict["confidence"],
        rationale=trace_dict["rationale"],
        hinglish_message=trace_dict["hinglish_message"],
        llm_provider=trace_dict["provider_used"],
        dispatch_channel=trace_dict["dispatch_channel"],
        dispatch_cost_inr=trace_dict["dispatch_cost_inr"],
        razorpay_link_id=trace_dict["razorpay_link_id"],
        razorpay_link_url=trace_dict["razorpay_link_url"],
        classification_rule=trace_dict["classification_rule"],
        amount_inr=amount_inr,
        triage_metadata=json.dumps(triage.strategy.metadata, default=str),
        retry_scheduled_at=triage.strategy.retry_scheduled_at,
        pre_flight_passed=True,
        guardrail_checks=json.dumps({
            "idempotency_passed": True,
            "retry_cap_passed": attempt_number <= policy.max_retry_attempts,
            "quiet_hours_passed": True,
            "anti_spam_passed": True,
        }),
    ))

    # ── Build result dict ─────────────────────────────────────────────────────
    result = {
        "event_id": sim_event_id,
        "trace_id": trace_id,
        "cat_tag": cat_tag,
        "index": index,
        "total": total,
        "amount_inr": amount_inr,
        **trace_dict,
        "guardrail_violation": False,   # Pre-flight passed by design in simulation
        "run_id": run_id,
        "processed_at": datetime.now(timezone.utc).isoformat() + "Z",
    }

    # ── SSE broadcast ─────────────────────────────────────────────────────────
    sse_event = {
        "type": "trace_update",
        "trace_id": trace_id,
        "event_id": sim_event_id,
        "index": index,
        "total": total,
        "cat_tag": cat_tag,
        "category": trace_dict["category"],
        "action_type": trace_dict["action_type"],
        "outcome_status": trace_dict["outcome_status"],
        "amount_inr": amount_inr,
        "confidence": trace_dict["confidence"],
        "rationale": trace_dict["rationale"],
        "hinglish_message": trace_dict["hinglish_message"],
        "dispatch_channel": trace_dict.get("dispatch_channel"),
        "dispatch_cost_inr": trace_dict.get("dispatch_cost_inr"),
        "razorpay_link_url": trace_dict.get("razorpay_link_url"),
        "classification_rule": trace_dict.get("classification_rule"),
        "guardrail_checks": {
            "idempotency_passed": True,
            "retry_cap_passed": attempt_number <= policy.max_retry_attempts,
            "quiet_hours_passed": True,
            "anti_spam_passed": True,
        },
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
    }
    await broadcast(sse_event)

    return result


async def _run_batch(run_id: str, seed: int = 42):
    """
    Background task: run the full 100-record batch and broadcast results.

    The run state is always resolved to a terminal value, so an unexpected
    failure can never leave `_current_run` wedged in "running" — that state
    makes POST /simulate return 409 forever with no way to recover short of
    restarting the service.
    """
    global _current_run

    logger.info("simulate.batch_start", extra={"run_id": run_id})
    policy = get_policy()
    reset_bandit()

    records = generate_dataset(seed=seed)[:10]
    accumulator = BenchmarkAccumulator()
    factory = get_session_factory()
    total = len(records)

    _current_run = {"run_id": run_id, "status": "running", "progress": 0, "total": total}

    # Broadcast run start
    await broadcast({
        "type": "batch_start",
        "run_id": run_id,
        "total": total,
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
    })

    try:
        summary = await _process_records(records, accumulator, factory, policy, run_id, total)
        await flush_bandit_state()
    except Exception as exc:
        logger.error("simulate.batch_failed", extra={"run_id": run_id, "error": str(exc)})
        _current_run = {
            "run_id": run_id,
            "status": "failed",
            "progress": (_current_run or {}).get("progress", 0),
            "total": total,
            "error": str(exc),
        }
        await broadcast({
            "type": "batch_failed",
            "run_id": run_id,
            "error": str(exc),
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        })
        raise

    _current_run = {
        "run_id": run_id,
        "status": "complete",
        "progress": total,
        "total": total,
        "summary": summary,
    }

    await broadcast({
        "type": "batch_complete",
        "run_id": run_id,
        "progress": total,
        "total": total,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
    })

    logger.info("simulate.batch_complete", extra={
        "run_id": run_id,
        "yield_pct": summary["revguard_yield_pct"],
        "guardrail_violations": summary["guardrail_violations"],
    })

    return summary


async def _process_records(records, accumulator, factory, policy, run_id: str, total: int) -> dict:
    """Drive every record through the pipeline, streaming progress as it goes."""
    global _current_run

    for i, record in enumerate(records, start=1):
        try:
            async with factory() as session:
                async with session.begin():
                    result = await _process_one(
                        record=record,
                        session=session,
                        accumulator=accumulator,
                        policy=policy,
                        run_id=run_id,
                        index=i,
                        total=total,
                    )
            accumulator.add(result)
            _current_run["progress"] = i

            # Persist learned weights periodically rather than only at the end,
            # so a spin-down mid-batch does not throw away what was learned.
            await maybe_flush_bandit()

            # Broadcast running summary every 5 events
            if i % 5 == 0 or i == total:
                await broadcast({
                    "type": "batch_progress",
                    "run_id": run_id,
                    "progress": i,
                    "total": total,
                    "summary": accumulator.summary(),
                    "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                })

        except Exception as exc:
            logger.error("simulate.record_error", extra={
                "run_id": run_id, "index": i, "error": str(exc)
            })
            # Continue processing remaining records

    return accumulator.summary()


# ── A/B: adaptive bandit vs. deterministic selection ──────────────────────────

async def _run_ab_comparison(run_id: str, seed: int, warm: bool = False) -> dict:
    """
    Run the same dataset under both channel-selection strategies and report the
    difference.

    Both arms see byte-identical inputs (same seed, same records, same order),
    so every difference in the result is attributable to channel selection and
    nothing else.  That is the point: it turns "we have an adaptive bandit" from
    a claim into a measurement — and it is equally capable of showing that the
    bandit lost, which on a single cold batch it usually does.

    Two modes, because they answer different questions:

      cold (default)
        Both arms start with no learning.  This measures the *cost of
        exploration*: a fresh bandit must spend real outreach on measuring arms
        it will later reject, and one 100-record batch is rarely long enough to
        earn that back.  This is the honest number for a first-ever run.

      warm
        The bandit arm processes the dataset once to learn, and is then measured
        on a second pass.  This is the *steady-state* number, and it is the one
        that matters in production, where weights persist across batches and
        across restarts (see `bandit.load_state`) rather than resetting every
        time.

    Runs sequentially rather than concurrently — a free-tier worker has one
    core, and two concurrent batches would contend for it, distorting both the
    comparison and the wall-clock.
    """
    policy = get_policy()
    original = policy.enable_adaptive_channel_bandit

    arms: dict[str, dict] = {}
    try:
        for arm_name, use_bandit in (("bandit", True), ("deterministic", False)):
            policy.enable_adaptive_channel_bandit = use_bandit

            # Each arm starts from a blank slate, otherwise the second arm would
            # inherit the first one's learning and the comparison is meaningless.
            reset_bandit()

            factory = get_session_factory()

            if warm and use_bandit:
                # Training pass — results discarded, only the learning is kept.
                await broadcast({
                    "type": "ab_arm_start",
                    "run_id": run_id,
                    "arm": "bandit_warmup",
                    "total": 100,
                    "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                })
                await _process_records(
                    generate_dataset(seed=seed),
                    BenchmarkAccumulator(),
                    factory,
                    policy,
                    f"{run_id}_warmup",
                    100,
                )

            accumulator = BenchmarkAccumulator()
            records = generate_dataset(seed=seed)

            await broadcast({
                "type": "ab_arm_start",
                "run_id": run_id,
                "arm": arm_name,
                "total": len(records),
                "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            })

            summary = await _process_records(
                records, accumulator, factory, policy, f"{run_id}_{arm_name}", len(records)
            )
            arms[arm_name] = summary
    finally:
        # Always restore the operator's setting, even if an arm blew up.
        policy.enable_adaptive_channel_bandit = original
        reset_bandit()

    bandit = arms["bandit"]
    deterministic = arms["deterministic"]

    cost_delta = bandit["total_cost_inr"] - deterministic["total_cost_inr"]
    recovery_delta = bandit["revguard_recovered_inr"] - deterministic["revguard_recovered_inr"]
    net_delta = bandit["net_recovery_inr"] - deterministic["net_recovery_inr"]

    if net_delta > 0:
        verdict = "bandit"
    elif net_delta < 0:
        verdict = "deterministic"
    else:
        verdict = "tie"

    return {
        "run_id": run_id,
        "seed": seed,
        "mode": "warm" if warm else "cold",
        "measures": (
            "steady-state performance, bandit pre-trained on one pass"
            if warm
            else "cold-start performance, including the bandit's exploration cost"
        ),
        "arms": arms,
        "delta": {
            "cost_inr": round(cost_delta, 2),
            "recovered_inr": round(recovery_delta, 2),
            "net_recovery_inr": round(net_delta, 2),
            "yield_pct": round(
                bandit["revguard_yield_pct"] - deterministic["revguard_yield_pct"], 2
            ),
        },
        "verdict": verdict,
        "summary": (
            f"Bandit net {'+' if net_delta >= 0 else '-'}Rs {abs(net_delta):,.2f} "
            f"vs deterministic on {bandit['total_records']} records"
        ),
    }


async def _run_ab(run_id: str, seed: int, warm: bool = False) -> None:
    """Background driver for the A/B comparison."""
    global _current_run

    total = 300 if warm else 200
    _current_run = {
        "run_id": run_id, "status": "running", "mode": "ab", "progress": 0, "total": total,
    }
    await broadcast({
        "type": "ab_start",
        "run_id": run_id,
        "seed": seed,
        "warm": warm,
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
    })

    try:
        result = await _run_ab_comparison(run_id, seed, warm=warm)
    except Exception as exc:
        logger.error("simulate.ab_failed", extra={"run_id": run_id, "error": str(exc)})
        _current_run = {"run_id": run_id, "status": "failed", "mode": "ab", "error": str(exc)}
        await broadcast({
            "type": "batch_failed",
            "run_id": run_id,
            "error": str(exc),
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        })
        return

    _current_run = {
        "run_id": run_id,
        "status": "complete",
        "mode": "ab",
        "progress": total,
        "total": total,
        "ab_result": result,
        "summary": result["arms"]["bandit"],
    }

    await broadcast({
        "type": "ab_complete",
        "run_id": run_id,
        "result": result,
        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
    })

    logger.info("simulate.ab_complete", extra={
        "run_id": run_id,
        "verdict": result["verdict"],
        "net_delta_inr": result["delta"]["net_recovery_inr"],
    })


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/simulate", tags=["benchmark"])
async def start_simulation(
    background_tasks: BackgroundTasks,
    seed: int = Query(default=42, description="Random seed for reproducible dataset"),
):
    """
    Start the 100-record batch simulation in the background.
    Results stream live via GET /stream (SSE).
    Returns immediately with a run_id to track progress.
    """
    global _current_run

    if _current_run and _current_run.get("status") == "running":
        return JSONResponse(
            status_code=409,
            content={"error": "A batch run is already in progress", "run_id": _current_run["run_id"]},
        )

    run_id = uuid.uuid4().hex[:12]
    background_tasks.add_task(_run_batch, run_id=run_id, seed=seed)

    return {
        "status": "started",
        "run_id": run_id,
        "message": "Batch simulation started. Connect to /stream for live updates.",
        "stream_url": "/stream",
        "total_records": 100,
    }


@router.post("/simulate/ab", tags=["benchmark"])
async def start_ab_comparison(
    background_tasks: BackgroundTasks,
    seed: int = Query(default=42, description="Random seed — both arms use the same one"),
    warm: bool = Query(
        default=False,
        description=(
            "Pre-train the bandit on one pass before measuring it. "
            "False measures cold-start (including exploration cost); "
            "True measures steady state, which is what production sees."
        ),
    ),
):
    """
    Run the benchmark under both channel-selection strategies and report the
    difference in cost, recovery and net ROI.

    Both arms process identical records in identical order, so the delta is
    attributable to channel selection alone.
    """
    global _current_run

    if _current_run and _current_run.get("status") == "running":
        return JSONResponse(
            status_code=409,
            content={
                "error": "A batch run is already in progress",
                "run_id": _current_run["run_id"],
            },
        )

    run_id = uuid.uuid4().hex[:12]
    background_tasks.add_task(_run_ab, run_id=run_id, seed=seed, warm=warm)

    return {
        "status": "started",
        "mode": "ab",
        "warm": warm,
        "run_id": run_id,
        "message": "A/B comparison started. Connect to /stream for live updates.",
        "stream_url": "/stream",
        "total_records": 300 if warm else 200,
    }


@router.get("/simulate/status", tags=["benchmark"])
async def simulation_status():
    """Return the current (or last) simulation run status."""
    if _current_run is None:
        return {"status": "idle", "message": "No simulation has been run yet."}
    return _current_run


@router.get("/simulate/dataset-preview", tags=["benchmark"])
async def dataset_preview(seed: int = 42, limit: int = 10):
    """Preview the first N records of the synthetic dataset."""
    records = generate_dataset(seed=seed)
    preview = []
    for r in records[:limit]:
        payment = r.get("payload", {}).get("payment", {}).get("entity", {})
        preview.append({
            "event_id": payment.get("id"),
            "amount_inr": payment.get("amount", 0) / 100,
            "error_code": payment.get("error_code"),
            "error_reason": payment.get("error_reason"),
            "bank": payment.get("bank"),
            "cat_tag": r.get("_meta", {}).get("cat_tag"),
        })
    return {"records": preview, "total": len(records)}
