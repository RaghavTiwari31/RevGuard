"""
Tests for the Issuer Health Radar (app/issuer_radar.py).

DoD: Simulating 30 consecutive failures from one issuer flips new
TRANSIENT_DOWNTIME events for that issuer into extended backoff.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from app.issuer_radar import (
    DEFAULT_SPIKE_THRESHOLD,
    get_bin_stats,
    is_in_extended_backoff,
    record_failure,
)


class TestRecordFailure:
    async def test_first_failure_not_in_backoff(self, async_session: AsyncSession):
        in_backoff = await record_failure(async_session, "411111")
        assert in_backoff is False

    async def test_none_bin_returns_false(self, async_session: AsyncSession):
        result = await record_failure(async_session, None)
        assert result is False

    async def test_spike_threshold_triggers_backoff(self, async_session: AsyncSession):
        """DoD: 30 consecutive failures → extended backoff."""
        bin_no = "424242"
        threshold = DEFAULT_SPIKE_THRESHOLD  # 30

        for i in range(threshold - 1):
            result = await record_failure(
                async_session, bin_no,
                spike_threshold=threshold,
                now=datetime.now(timezone.utc) + timedelta(seconds=i),
            )
            assert result is False, f"Should not be in backoff after {i+1} failures"

        # The 30th failure should trigger backoff
        result = await record_failure(
            async_session, bin_no,
            spike_threshold=threshold,
            now=datetime.now(timezone.utc) + timedelta(seconds=threshold),
        )
        assert result is True, "Expected backoff after 30 failures"

    async def test_custom_threshold(self, async_session: AsyncSession):
        bin_no = "555500"
        threshold = 5

        for i in range(threshold - 1):
            r = await record_failure(async_session, bin_no,
                                     spike_threshold=threshold,
                                     now=datetime.now(timezone.utc) + timedelta(seconds=i))
            assert r is False

        # 5th failure
        r = await record_failure(async_session, bin_no,
                                 spike_threshold=threshold,
                                 now=datetime.now(timezone.utc) + timedelta(seconds=threshold))
        assert r is True

    async def test_window_reset_clears_counter(self, async_session: AsyncSession):
        """Failures outside the rolling window don't count toward threshold."""
        bin_no = "600000"
        threshold = 5
        window_minutes = 15

        now = datetime.now(timezone.utc)

        # Record 4 failures at time T
        for i in range(4):
            await record_failure(async_session, bin_no,
                                 spike_threshold=threshold, window_minutes=window_minutes,
                                 now=now + timedelta(seconds=i))

        # Record 1 failure well outside the window (20 minutes later)
        outside_window = now + timedelta(minutes=20)
        result = await record_failure(async_session, bin_no,
                                      spike_threshold=threshold, window_minutes=window_minutes,
                                      now=outside_window)
        # Counter reset → only 1 failure in new window → no backoff
        assert result is False


class TestIsInExtendedBackoff:
    async def test_not_in_backoff_initially(self, async_session: AsyncSession):
        result = await is_in_extended_backoff(async_session, "700700")
        assert result is False

    async def test_in_backoff_after_spike(self, async_session: AsyncSession):
        bin_no = "800800"
        threshold = 3

        for i in range(threshold):
            await record_failure(async_session, bin_no,
                                 spike_threshold=threshold,
                                 now=datetime.now(timezone.utc) + timedelta(seconds=i))

        result = await is_in_extended_backoff(async_session, bin_no)
        assert result is True

    async def test_not_in_backoff_after_expiry(self, async_session: AsyncSession):
        bin_no = "900900"
        threshold = 3
        now = datetime.now(timezone.utc)

        for i in range(threshold):
            await record_failure(async_session, bin_no,
                                 spike_threshold=threshold,
                                 extended_backoff_hours=1,
                                 now=now + timedelta(seconds=i))

        # Check backoff 2 hours after it started (backoff window was 1 hour)
        future = now + timedelta(hours=2)
        result = await is_in_extended_backoff(async_session, bin_no, now=future)
        assert result is False


class TestGetBinStats:
    async def test_returns_none_for_unknown_bin(self, async_session: AsyncSession):
        result = await get_bin_stats(async_session, "000000")
        assert result is None

    async def test_returns_stats_after_failures(self, async_session: AsyncSession):
        bin_no = "111111"
        await record_failure(async_session, bin_no, spike_threshold=100)
        await record_failure(async_session, bin_no, spike_threshold=100)

        stats = await get_bin_stats(async_session, bin_no)
        assert stats is not None
        assert stats.total_failures == 2
