"""
RevGuard — Epsilon-Greedy Adaptive Channel Bandit

A multi-armed bandit that learns which outreach channel (SMS / WhatsApp / Voice)
gets the best "payment recovery" response rate per customer segment.

Algorithm: epsilon-greedy
  - With probability epsilon → explore (pick a random channel)
  - With probability (1 - epsilon) → exploit (pick the best-performing channel so far)

Arms: SMS, WhatsApp, Voice
Reward: 1.0 if the customer pays after outreach, 0.0 otherwise
       (In the simulation, rewards are synthetic but follow realistic distributions)

Segments: by failure category (TEMPORARY_CASHFLOW, EXPIRED_MANDATE, TRANSIENT_DOWNTIME)

Persistence
-----------
Arm statistics are held in memory for speed and mirrored to the
`bandit_arm_stats` table.  The deployment target idles the process out after
~15 minutes, so a purely in-memory bandit would reset to zero on every cold
start and could never actually learn anything.  `load_state()` restores the
weights at boot and `flush_state()` writes them back; individual reward updates
are also persisted, throttled so a 100-record batch does not turn into 300
round-trips.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Optional

from app.channels import Channel
from app.logging_config import get_logger

logger = get_logger(__name__)

EPSILON = 0.15          # Starting exploration rate
MIN_EPSILON = 0.02      # Floor — never stop exploring entirely
EPSILON_DECAY_SCALE = 40  # Selections per halving of the exploration rate
MIN_TRIALS = 3          # Minimum selections before an arm can be judged


def effective_epsilon(total_selections: int, base: float = EPSILON) -> float:
    """
    Exploration rate, decayed by how much the segment has already seen.

    A fixed rate never stops paying for exploration: at 15% forever, roughly one
    outreach in seven is deliberately spent on an arm already known to be worse,
    and on high-value tickets that is expensive enough to lose to a plain
    heuristic. Decaying it keeps early learning fast while letting a mature
    segment settle onto its best arm — without ever reaching zero, so a genuine
    shift in channel performance can still be discovered.
    """
    decayed = base / (1.0 + total_selections / EPSILON_DECAY_SCALE)
    return max(MIN_EPSILON, decayed)


@dataclass
class ArmStats:
    """
    Two independent counters, because selection and feedback are not simultaneous.

    `selections` advances the moment we choose an arm; `pulls` advances only when
    a reward actually comes back.  Warm-up has to read `selections` — if it read
    `pulls`, an arm selected but not yet scored would still look unmeasured and
    would be selected again, and again, forever.
    """
    selections: int = 0
    pulls: int = 0
    total_reward: float = 0.0

    @property
    def mean_reward(self) -> float:
        return self.total_reward / self.pulls if self.pulls > 0 else 0.0


@dataclass
class BanditState:
    """Per-segment bandit state."""
    segment: str
    arms: dict[Channel, ArmStats] = field(default_factory=lambda: {
        Channel.SMS: ArmStats(),
        Channel.WHATSAPP: ArmStats(),
        Channel.VOICE: ArmStats(),
    })
    total_pulls: int = 0
    total_selections: int = 0


# Global bandit states, keyed by segment name
_states: dict[str, BanditState] = {}

# Arms changed since the last flush, as (segment, channel) pairs.
_dirty: set[tuple[str, str]] = set()

# Flush after this many reward updates. Small enough that a crash loses almost
# nothing, large enough that a batch run is not dominated by database writes.
_FLUSH_EVERY = 25
_since_flush = 0


def _get_state(segment: str) -> BanditState:
    if segment not in _states:
        _states[segment] = BanditState(segment=segment)
    return _states[segment]


def select_channel_bandit(
    segment: str,
    eligible: Sequence[Channel],
    epsilon: Optional[float] = None,
) -> Channel:
    """
    Epsilon-greedy channel selection over the *eligible* channels for this event.

    Eligibility is decided upstream by policy (see `channels.eligible_channels`)
    — the bandit only ever chooses among arms it is allowed to pull, so it can
    never learn its way around a guardrail.

    Segment is typically the failure category name.

    Three phases, in order:
      1. Warm-up  — any eligible arm below MIN_TRIALS is pulled first, least-
         pulled arm wins.  This guarantees every arm is measured before any is
         judged, without ever letting an unmeasured arm win on a sentinel score.
      2. Explore  — with probability epsilon, pick uniformly at random. The
         rate decays as the segment accumulates evidence (see
         `effective_epsilon`); pass `epsilon` explicitly to override.
      3. Exploit  — highest observed mean reward, ties broken by more evidence.
    """
    eligible = list(eligible)
    if not eligible:
        return Channel.NONE

    state = _get_state(segment)

    if len(eligible) == 1:
        return _record_selection(state, eligible[0], "forced")

    # ── Phase 1: warm-up — measure every arm before trusting any of them ─────
    under_explored = [c for c in eligible if state.arms[c].selections < MIN_TRIALS]
    if under_explored:
        chosen = min(under_explored, key=lambda c: state.arms[c].selections)
        return _record_selection(state, chosen, "warmup")

    # ── Phase 2: explore ─────────────────────────────────────────────────────
    if epsilon is None:
        epsilon = effective_epsilon(state.total_selections)
    if random.random() < epsilon:
        return _record_selection(state, random.choice(eligible), "explore")

    # ── Phase 3: exploit — best mean reward, more evidence breaks ties ───────
    best_channel = max(
        eligible,
        key=lambda c: (state.arms[c].mean_reward, state.arms[c].pulls),
    )
    return _record_selection(state, best_channel, "exploit")


def _record_selection(state: BanditState, channel: Channel, phase: str) -> Channel:
    """Count the selection and log which phase produced it."""
    arm = state.arms[channel]
    arm.selections += 1
    state.total_selections += 1
    _dirty.add((state.segment, channel.value))

    logger.debug(f"bandit.{phase}", extra={
        "segment": state.segment,
        "chosen": channel.value,
        "selections": arm.selections,
        "mean_reward": arm.mean_reward,
    })
    return channel


def record_reward(segment: str, channel: Channel, reward: float) -> None:
    """Feed back a reward signal to update the arm's running average."""
    global _since_flush

    state = _get_state(segment)
    arm = state.arms[channel]
    arm.pulls += 1
    arm.total_reward += reward
    state.total_pulls += 1

    _dirty.add((segment, channel.value))
    _since_flush += 1

    logger.debug("bandit.reward", extra={
        "segment": segment,
        "channel": channel.value,
        "reward": reward,
        "new_mean": arm.mean_reward,
        "total_pulls": state.total_pulls,
    })


async def maybe_flush() -> bool:
    """
    Persist pending arm updates once enough have accumulated.

    Called from the batch runner after each record.  Returns True if a flush
    actually happened.
    """
    global _since_flush
    if _since_flush < _FLUSH_EVERY:
        return False
    _since_flush = 0
    await flush_state()
    return True


def get_bandit_stats() -> dict:
    """Return the current bandit state for all segments (dashboard display)."""
    return {
        segment: {
            channel.value: {
                "selections": arm.selections,
                "pulls": arm.pulls,
                "mean_reward": round(arm.mean_reward, 3),
                "total_reward": round(arm.total_reward, 2),
            }
            for channel, arm in state.arms.items()
        }
        for segment, state in _states.items()
    }


def reset_bandit() -> None:
    """
    Reset in-memory bandit state (used between benchmark runs).

    Deliberately leaves the persisted table alone — see `clear_persisted_state`
    for the destructive version.
    """
    global _since_flush
    _states.clear()
    _dirty.clear()
    _since_flush = 0


# ── Persistence ───────────────────────────────────────────────────────────────

def _channel_from_value(value: str) -> Channel | None:
    try:
        return Channel(value)
    except ValueError:
        return None


async def load_state() -> dict:
    """
    Restore arm statistics from the database into memory.

    Called once at startup.  Rows for channels the code no longer knows about
    are ignored rather than crashing the boot.
    """
    from sqlalchemy import select

    from app.db import BanditArmStat, get_session_factory

    reset_bandit()
    segments = 0
    arms = 0

    try:
        factory = get_session_factory()
        async with factory() as session:
            rows = (await session.execute(select(BanditArmStat))).scalars().all()
            for row in rows:
                channel = _channel_from_value(row.channel)
                if channel is None or channel not in _get_state(row.segment).arms:
                    continue
                state = _get_state(row.segment)
                arm = state.arms[channel]
                arm.selections = row.selections or 0
                arm.pulls = row.pulls or 0
                arm.total_reward = row.total_reward or 0.0
                state.total_pulls += arm.pulls
                state.total_selections += arm.selections
                arms += 1
            segments = len(_states)
    except Exception as exc:
        # A bandit that cannot load its history is a bandit that starts cold —
        # degraded, but never a reason to fail startup.
        logger.warning("bandit.load_failed", extra={"error": str(exc)})
        return {"segments": 0, "arms": 0, "loaded": False}

    _dirty.clear()
    return {"segments": segments, "arms": arms, "loaded": True}


async def flush_state() -> int:
    """
    Write pending in-memory arm statistics back to the database.

    Returns the number of arms written.  Upserts by (segment, channel).
    """
    from sqlalchemy import select

    from app.db import BanditArmStat, get_session_factory

    if not _dirty:
        return 0

    pending = list(_dirty)
    written = 0

    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            for segment, channel_value in pending:
                channel = _channel_from_value(channel_value)
                state = _states.get(segment)
                if channel is None or state is None or channel not in state.arms:
                    continue
                arm = state.arms[channel]

                row = (
                    await session.execute(
                        select(BanditArmStat).where(
                            BanditArmStat.segment == segment,
                            BanditArmStat.channel == channel_value,
                        )
                    )
                ).scalars().first()

                if row is None:
                    row = BanditArmStat(segment=segment, channel=channel_value)
                    session.add(row)

                row.selections = arm.selections
                row.pulls = arm.pulls
                row.total_reward = arm.total_reward
                written += 1

    _dirty.difference_update(pending)
    logger.info("bandit.flushed", extra={"arms_written": written})
    return written


async def clear_persisted_state() -> int:
    """Delete all persisted arm statistics. Used when resetting a benchmark."""
    from sqlalchemy import delete

    from app.db import BanditArmStat, get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        async with session.begin():
            result = await session.execute(delete(BanditArmStat))
    reset_bandit()
    return result.rowcount or 0
