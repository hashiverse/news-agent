"""Conditional-GET RSS fetcher backed by the ``feed_cache`` SQLite table.

For each source URL the fetcher does:

1. Look up the previously-cached body and ETag/Last-Modified from
   :mod:`news_agent.feed_cache_db`.
2. Issue an HTTPS ``GET`` with ``If-None-Match`` / ``If-Modified-Since``
   headers when the cache supplies them.
3. On ``200 OK`` write the new body + new headers + ``fetched_at`` to the
   cache and return the body.
4. On ``304 Not Modified`` bump ``fetched_at`` only and return the cached
   body.
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


class FeedFetchError(RuntimeError):
    """Raised when a feed cannot be fetched and no cached body is available."""


def fetch_feed_body(
    source_url: str,
    conn: sqlite3.Connection,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: urllib.request.OpenerDirector | None = None,
    now_unix: int | None = None,
) -> bytes:
    """Return the (possibly cached) body of ``source_url``.

    Raises :class:`FeedFetchError` only when fetching fails *and* nothing is
    cached. Transient errors over a populated cache return the stale body
    with a logged warning.
    """
    if now_unix is None:
        now_unix = int(time.time())

    cached = get_cached(conn, source_url)

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
        )
        logger.info("fetched %s (%d bytes)", source_url, len(body))
        return body
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached is not None:
            update_fetched_at(conn, source_url, now_unix)
            logger.debug("304 not modified: %s", source_url)
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
