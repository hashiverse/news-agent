"""Per-source feed cache table access.

The ``feed_cache`` table stores the last-fetched body of each RSS source
URL alongside the conditional-GET state (``etag``, ``last_modified``) so the
next fetch can issue ``If-None-Match`` / ``If-Modified-Since`` and avoid
re-downloading unchanged feeds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class CachedFeed:
    """One row from the ``feed_cache`` table."""

    source_url: str
    body: bytes
    etag: str | None
    last_modified: str | None
    fetched_at_unix: int
    cache_valid_until_unix: int


def get_cached(
    conn: sqlite3.Connection, source_url: str
) -> CachedFeed | None:
    """Return the cached feed body for ``source_url`` or ``None``."""
    row = conn.execute(
        """
        SELECT source_url, body, etag, last_modified, fetched_at_unix,
               cache_valid_until_unix
        FROM feed_cache
        WHERE source_url = ?
        """,
        (source_url,),
    ).fetchone()
    if row is None:
        return None
    return CachedFeed(
        source_url=row[0],
        body=row[1],
        etag=row[2],
        last_modified=row[3],
        fetched_at_unix=row[4],
        cache_valid_until_unix=row[5],
    )


def save_cache(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    body: bytes,
    etag: str | None,
    last_modified: str | None,
    fetched_at_unix: int,
    cache_valid_until_unix: int,
) -> None:
    """Insert or replace the cache row for ``source_url``."""
    conn.execute(
        """
        INSERT OR REPLACE INTO feed_cache (
            source_url, body, etag, last_modified, fetched_at_unix,
            cache_valid_until_unix
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source_url,
            body,
            etag,
            last_modified,
            fetched_at_unix,
            cache_valid_until_unix,
        ),
    )


def update_fetched_at(
    conn: sqlite3.Connection,
    source_url: str,
    fetched_at_unix: int,
    cache_valid_until_unix: int,
) -> None:
    """Bump ``fetched_at_unix`` and refresh ``cache_valid_until_unix`` without
    touching body or headers (for 304 hits — the body we already have is still
    current, so we extend its validity window)."""
    conn.execute(
        "UPDATE feed_cache "
        "SET fetched_at_unix = ?, cache_valid_until_unix = ? "
        "WHERE source_url = ?",
        (fetched_at_unix, cache_valid_until_unix, source_url),
    )
