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

State is stored in-memory (resets on restart) — Phase 4 can persist to DB.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from app.channels import Channel
from app.logging_config import get_logger

logger = get_logger(__name__)

EPSILON = 0.15          # 15% exploration rate
MIN_TRIALS = 3          # Minimum pulls before exploitation kicks in per arm


@dataclass
class ArmStats:
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


# Global bandit states, keyed by segment name
_states: dict[str, BanditState] = {}


def _get_state(segment: str) -> BanditState:
    if segment not in _states:
        _states[segment] = BanditState(segment=segment)
    return _states[segment]


def select_channel_bandit(
    segment: str,
    amount_inr: float,
    epsilon: float = EPSILON,
) -> Channel:
    """
    Epsilon-greedy channel selection.
    Segment is typically the failure category name.
    Voice is only eligible for amounts >= ₹100.
    """
    state = _get_state(segment)
    eligible = [Channel.SMS, Channel.WHATSAPP]
    if amount_inr >= 100:
        eligible.append(Channel.VOICE)

    # Explore
    if random.random() < epsilon or state.total_pulls < MIN_TRIALS * len(eligible):
        chosen = random.choice(eligible)
        logger.debug("bandit.explore", extra={
            "segment": segment, "chosen": chosen.value, "epsilon": epsilon
        })
        return chosen

    # Exploit — pick arm with highest mean reward (among eligible arms with pulls)
    best_channel = max(
        (c for c in eligible),
        key=lambda c: state.arms[c].mean_reward if state.arms[c].pulls >= MIN_TRIALS else float("inf"),
    )
    logger.debug("bandit.exploit", extra={
        "segment": segment,
        "chosen": best_channel.value,
        "mean_reward": state.arms[best_channel].mean_reward,
    })
    return best_channel


def record_reward(segment: str, channel: Channel, reward: float) -> None:
    """Feed back a reward signal to update the arm's running average."""
    state = _get_state(segment)
    arm = state.arms[channel]
    arm.pulls += 1
    arm.total_reward += reward
    state.total_pulls += 1

    logger.debug("bandit.reward", extra={
        "segment": segment,
        "channel": channel.value,
        "reward": reward,
        "new_mean": arm.mean_reward,
        "total_pulls": state.total_pulls,
    })


def get_bandit_stats() -> dict:
    """Return the current bandit state for all segments (dashboard display)."""
    return {
        segment: {
            channel.value: {
                "pulls": arm.pulls,
                "mean_reward": round(arm.mean_reward, 3),
                "total_reward": round(arm.total_reward, 2),
            }
            for channel, arm in state.arms.items()
        }
        for segment, state in _states.items()
    }


def reset_bandit() -> None:
    """Reset all bandit state (used between benchmark runs)."""
    _states.clear()
