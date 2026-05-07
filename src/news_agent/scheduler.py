"""Per-identity next-allowed-post-time computation.

Pure function — no DB, no clock, no random global state. The caller passes
``now_unix``, the identity's recent posts, and a ``rng`` (for jitter), and
gets back the integer Unix timestamp at which the identity is next eligible
to post.

Algorithm: even spacing across the day, with ±10% jitter on the spacing.

- ``target_interval = 24h / max_posts_per_day``
- If the identity has no posts in the last 24h:
    ``next = now + small_jitter`` (post-immediately, but stagger when many
    identities just started up)
- If the identity has posted but is under cap:
    ``next = max(now, last_post + target_interval) + jitter``
- If the identity is at cap:
    ``next = oldest_post_in_24h + 24h + jitter``

The "soonest next" identity drives the main loop's wait-and-then-post cycle.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from news_agent.posts_db import ONE_DAY_SECONDS, PostRecord

JITTER_FRACTION = 0.10
NO_HISTORY_JITTER_RANGE_SECONDS = 60  # ±60s when an identity has zero history


def compute_next_post_time(
    *,
    max_posts_per_day: int,
    posts_in_last_24h: Sequence[PostRecord],
    now_unix: int,
    rng: random.Random,
) -> int:
    """Return the Unix timestamp at which this identity may next post.

    ``posts_in_last_24h`` MUST be sorted oldest-first (which is what
    :func:`posts_db.posts_in_last_24h_for_identity` returns).
    """
    if max_posts_per_day <= 0:
        # An operator who sets max_posts_per_day=0 has explicitly disabled
        # posting for this identity. Push it 24h out so the scheduler keeps
        # picking other identities.
        return now_unix + ONE_DAY_SECONDS

    target_interval = ONE_DAY_SECONDS // max_posts_per_day

    if not posts_in_last_24h:
        jitter = rng.randint(
            -NO_HISTORY_JITTER_RANGE_SECONDS, NO_HISTORY_JITTER_RANGE_SECONDS
        )
        return max(now_unix, now_unix + jitter)

    if len(posts_in_last_24h) >= max_posts_per_day:
        # At cap: must wait for the oldest post in the last 24h to expire.
        oldest = posts_in_last_24h[0].posted_at_unix
        base = oldest + ONE_DAY_SECONDS
    else:
        last = posts_in_last_24h[-1].posted_at_unix
        base = max(now_unix, last + target_interval)

    jitter_amplitude = max(1, int(target_interval * JITTER_FRACTION))
    jitter = rng.randint(-jitter_amplitude, jitter_amplitude)
    return base + jitter
