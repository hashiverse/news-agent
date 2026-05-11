"""Conditional-GET RSS fetcher backed by the ``feed_cache`` SQLite table.

For each source URL the fetcher does:

0. If the cached body's per-row ``cache_valid_until_unix`` is still in the
   future, return it immediately — no network call. Each cache write picks
   a uniformly random expiry in `[now+20min, now+40min]` (the validity
   window) so feeds fetched together don't all expire at the same instant
   and stampede the upstream when they revalidate. This also covers the
   case where a server doesn't honour ``If-None-Match`` / ``If-Modified-Since``
   and always returns 200 — without the cache we'd burn full bandwidth
   every iteration.
1. Look up the previously-cached body and ETag/Last-Modified from
   :mod:`news_agent.feed_cache_db`.
2. Issue an HTTPS ``GET`` with ``If-None-Match`` / ``If-Modified-Since``
   headers when the cache supplies them.
3. On ``200 OK`` write the new body + new headers + ``fetched_at`` + a
   freshly-randomized ``cache_valid_until_unix`` to the cache and return
   the body.
4. On ``304 Not Modified`` bump ``fetched_at`` and refresh
   ``cache_valid_until_unix`` only — the cached body is still good.
5. On network or HTTP errors:
   - return the stale cached body when one exists,
   - re-raise when there's no cache to fall back to.

The implementation deliberately mirrors the conditional-GET path in
:mod:`news_agent.remote_source` (which fetches the control file). The
difference is the *destination* of the cached body: there it lives on
disk + a JSON sidecar, here it lives in SQLite.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
import urllib.error
import urllib.request

from news_agent.feed_cache_db import (
    get_cached,
    save_cache,
    update_fetched_at,
)

logger = logging.getLogger(__name__)

USER_AGENT = "news-agent/0.1 (+https://github.com/hashiverse/news-agent)"
DEFAULT_TIMEOUT_SECONDS = 30.0

# Per-row randomized cache validity. A new cache write picks a uniformly
# random target between MIN and MAX seconds in the future. With many feeds
# fetched together (e.g. at daemon startup), this naturally sprawls their
# next-revalidation times across a 20-minute window — preventing the
# synchronized re-fetch storm a fixed window would cause every 30 min,
# which would otherwise hammer shared upstreams (e.g. multiple YouTube
# channel feeds all hitting youtube.com simultaneously).
CACHE_VALIDITY_MIN_SECONDS = 20 * 60
CACHE_VALIDITY_MAX_SECONDS = 40 * 60


class FeedFetchError(RuntimeError):
    """Raised when a feed cannot be fetched and no cached body is available."""


def _next_cache_valid_until(now_unix: int, rng: random.Random) -> int:
    """Return a randomized validity-end timestamp ``now + uniform(MIN, MAX)``."""
    return now_unix + rng.randint(
        CACHE_VALIDITY_MIN_SECONDS, CACHE_VALIDITY_MAX_SECONDS
    )


def fetch_feed_body(
    source_url: str,
    conn: sqlite3.Connection,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: urllib.request.OpenerDirector | None = None,
    now_unix: int | None = None,
    rng: random.Random | None = None,
) -> bytes:
    """Return the (possibly cached) body of ``source_url``.

    Raises :class:`FeedFetchError` only when fetching fails *and* nothing is
    cached. Transient errors over a populated cache return the stale body
    with a logged warning.

    ``rng`` controls the randomized cache-validity jitter on writes. Defaults
    to a fresh ``random.Random()`` (non-deterministic — fine for production).
    Tests should pass an explicit seeded ``random.Random(seed)``.
    """
    if now_unix is None:
        now_unix = int(time.time())
    if rng is None:
        rng = random.Random()

    cached = get_cached(conn, source_url)

    # Short-circuit: cache is still inside its randomized validity window →
    # return it without any network request. Logged at DEBUG (not INFO)
    # because this is the boring steady-state path — the runner re-fetches
    # every source on every iteration, and we don't want to spam stderr
    # with "skipped network" lines. Real network events (200, 304) stay at INFO.
    if cached is not None and now_unix < cached.cache_valid_until_unix:
        cache_age = max(0, now_unix - cached.fetched_at_unix)
        valid_for = cached.cache_valid_until_unix - now_unix
        logger.debug(
            "fetched %s (%d bytes, from cache, age %ds, valid for another %ds — skipped network)",
            source_url,
            len(cached.body),
            cache_age,
            valid_for,
        )
        return cached.body

    request = urllib.request.Request(
        source_url, headers={"User-Agent": USER_AGENT}
    )
    if cached is not None:
        if cached.etag:
            request.add_header("If-None-Match", cached.etag)
        if cached.last_modified:
            request.add_header("If-Modified-Since", cached.last_modified)

    open_func = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_func(request, timeout=timeout) as response:
            body = response.read()
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
        save_cache(
            conn,
            source_url=source_url,
            body=body,
            etag=etag,
            last_modified=last_modified,
            fetched_at_unix=now_unix,
            cache_valid_until_unix=_next_cache_valid_until(now_unix, rng),
        )
        logger.info(
            "fetched %s (%d bytes, downloaded)", source_url, len(body)
        )
        return body
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached is not None:
            update_fetched_at(
                conn,
                source_url,
                now_unix,
                _next_cache_valid_until(now_unix, rng),
            )
            cache_age = max(0, now_unix - cached.fetched_at_unix)
            logger.info(
                "fetched %s (%d bytes, from cache, age %ds, 304 not modified)",
                source_url,
                len(cached.body),
                cache_age,
            )
            return cached.body
        return _fall_back_or_raise(source_url, cached, f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        return _fall_back_or_raise(source_url, cached, f"network error: {exc}")


def _fall_back_or_raise(source_url: str, cached, reason: str) -> bytes:
    if cached is not None:
        logger.warning(
            "fetching %s failed (%s) — using stale cache (age %ds)",
            source_url,
            reason,
            int(time.time()) - cached.fetched_at_unix,
        )
        return cached.body
    logger.error(
        "fetching %s failed (%s) and no cache exists", source_url, reason
    )
    raise FeedFetchError(f"could not fetch {source_url}: {reason}")
