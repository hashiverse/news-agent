"""Tests for scheduler.compute_next_post_time."""

from __future__ import annotations

import random

import pytest

from news_agent.posts_db import ONE_DAY_SECONDS, PostRecord
from news_agent.scheduler import (
    JITTER_FRACTION,
    NO_HISTORY_JITTER_RANGE_SECONDS,
    compute_next_post_time,
)


def _post(when: int) -> PostRecord:
    return PostRecord(
        posted_at_unix=when,
        identity_salt="any",
        canonical_url="https://x/" + str(when),
        source_url="https://feed/",
        title="t",
        item_guid=None,
        is_dry_run=False,
    )


def _seeded_rng() -> random.Random:
    return random.Random(0)


def test_no_history_returns_around_now():
    now = 10_000
    next_t = compute_next_post_time(
        max_posts_per_day=24,
        posts_in_last_24h=[],
        now_unix=now,
        rng=_seeded_rng(),
    )
    assert now - NO_HISTORY_JITTER_RANGE_SECONDS <= next_t <= now + NO_HISTORY_JITTER_RANGE_SECONDS


def test_under_cap_uses_target_interval_from_last_post():
    """24 posts/day → 1h interval. Last post was at now-30min → next at last+1h."""
    now = 1_000_000
    last = now - 30 * 60
    target = ONE_DAY_SECONDS // 24
    next_t = compute_next_post_time(
        max_posts_per_day=24,
        posts_in_last_24h=[_post(last)],
        now_unix=now,
        rng=_seeded_rng(),
    )
    expected_base = last + target
    jitter_amp = int(target * JITTER_FRACTION)
    assert expected_base - jitter_amp <= next_t <= expected_base + jitter_amp


def test_under_cap_but_last_post_older_than_interval_returns_now_plus_jitter():
    """If we're already 'overdue' (last post longer ago than interval), post now-ish."""
    now = 1_000_000
    last = now - 5 * ONE_DAY_SECONDS // 24  # 5h ago, target is 1h
    target = ONE_DAY_SECONDS // 24
    next_t = compute_next_post_time(
        max_posts_per_day=24,
        posts_in_last_24h=[_post(last)],
        now_unix=now,
        rng=_seeded_rng(),
    )
    jitter_amp = int(target * JITTER_FRACTION)
    assert now - jitter_amp <= next_t <= now + jitter_amp


def test_at_cap_waits_until_oldest_expires():
    """5 posts in last 24h, cap is 5. Oldest at now-23h → next at oldest+24h."""
    now = 1_000_000
    target = ONE_DAY_SECONDS // 5
    posts = [
        _post(now - 23 * 3600),   # oldest
        _post(now - 18 * 3600),
        _post(now - 12 * 3600),
        _post(now - 6 * 3600),
        _post(now - 1 * 3600),    # newest
    ]
    next_t = compute_next_post_time(
        max_posts_per_day=5,
        posts_in_last_24h=posts,
        now_unix=now,
        rng=_seeded_rng(),
    )
    expected_base = (now - 23 * 3600) + ONE_DAY_SECONDS
    jitter_amp = int(target * JITTER_FRACTION)
    assert expected_base - jitter_amp <= next_t <= expected_base + jitter_amp


def test_cap_zero_pushes_24h_out():
    """max_posts_per_day=0 effectively disables posting for the identity."""
    now = 1_000_000
    next_t = compute_next_post_time(
        max_posts_per_day=0,
        posts_in_last_24h=[],
        now_unix=now,
        rng=_seeded_rng(),
    )
    assert next_t == now + ONE_DAY_SECONDS


def test_jitter_makes_consecutive_calls_different():
    now = 1_000_000
    rng_a = random.Random(42)
    rng_b = random.Random(43)
    a = compute_next_post_time(
        max_posts_per_day=24,
        posts_in_last_24h=[_post(now - 3000)],
        now_unix=now,
        rng=rng_a,
    )
    b = compute_next_post_time(
        max_posts_per_day=24,
        posts_in_last_24h=[_post(now - 3000)],
        now_unix=now,
        rng=rng_b,
    )
    assert a != b


def test_seeded_rng_makes_calls_deterministic():
    now = 1_000_000
    posts = [_post(now - 3000)]
    a = compute_next_post_time(
        max_posts_per_day=24,
        posts_in_last_24h=posts,
        now_unix=now,
        rng=random.Random(42),
    )
    b = compute_next_post_time(
        max_posts_per_day=24,
        posts_in_last_24h=posts,
        now_unix=now,
        rng=random.Random(42),
    )
    assert a == b


@pytest.mark.parametrize("cap", [1, 5, 24, 100])
def test_jitter_amplitude_scales_with_interval(cap: int):
    """Jitter is bounded by ±10% of the target interval."""
    now = 1_000_000
    target = ONE_DAY_SECONDS // cap
    expected_amp = max(1, int(target * JITTER_FRACTION))

    deltas: list[int] = []
    for seed in range(50):
        rng = random.Random(seed)
        # Use a last-post that puts the base at last+target = now (so deltas
        # are pure jitter relative to `now`).
        last = now - target
        next_t = compute_next_post_time(
            max_posts_per_day=cap,
            posts_in_last_24h=[_post(last)],
            now_unix=now,
            rng=rng,
        )
        deltas.append(next_t - now)
    assert all(-expected_amp <= d <= expected_amp for d in deltas), deltas
