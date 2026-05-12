"""Posts-history table access.

The ``posts`` table records every post the daemon has made (or *would have*
made, in dry-run mode). Two query shapes drive the rest of the system:

- :func:`posted_canonical_urls_in_last_24h` — used as the cross-identity
  dedupe filter when an identity's article picker chooses a candidate.
- :func:`posts_in_last_24h_for_identity` — used by the scheduler to compute
  per-identity next-allowed-post times.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

ONE_DAY_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class PostRecord:
    """One row from the ``posts`` table."""

    posted_at_unix: int
    identity_salt: str
    canonical_url: str
    source_url: str
    title: str
    item_guid: str | None
    is_dry_run: bool
    is_skipped: bool = False


def record_post(
    conn: sqlite3.Connection,
    *,
    posted_at_unix: int,
    identity_salt: str,
    canonical_url: str,
    source_url: str,
    title: str,
    item_guid: str | None,
    is_dry_run: bool,
    is_skipped: bool = False,
) -> None:
    """Append a row to the posts history.

    ``is_skipped=True`` marks a row that was decided-against (e.g. preview
    failed the validity rule). Skipped rows still count toward the picker's
    cross-identity dedupe (so we don't re-fetch the same dud URL every cycle)
    but are excluded from the scheduler's per-identity 24h quota count.
    """
    conn.execute(
        """
        INSERT INTO posts (
            posted_at_unix, identity_salt, canonical_url, source_url,
            title, item_guid, is_dry_run, is_skipped
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            posted_at_unix,
            identity_salt,
            canonical_url,
            source_url,
            title,
            item_guid,
            1 if is_dry_run else 0,
            1 if is_skipped else 0,
        ),
    )


def posted_canonical_urls_in_last_24h(
    conn: sqlite3.Connection, now_unix: int
) -> set[str]:
    """Return every canonical URL posted by *any* identity in the last 24h.

    The article picker uses this set as the cross-identity dedupe filter.
    """
    cutoff = now_unix - ONE_DAY_SECONDS
    rows = conn.execute(
        "SELECT DISTINCT canonical_url FROM posts WHERE posted_at_unix >= ?",
        (cutoff,),
    )
    return {row[0] for row in rows}


def posts_in_last_24h_for_identity(
    conn: sqlite3.Connection, identity_salt: str, now_unix: int
) -> list[PostRecord]:
    """Return all *actual* posts by ``identity_salt`` in the last 24h, oldest first.

    Skipped rows are filtered out here: they didn't consume a post slot, so the
    scheduler shouldn't count them toward the per-identity daily quota.
    """
    cutoff = now_unix - ONE_DAY_SECONDS
    rows = conn.execute(
        """
        SELECT posted_at_unix, identity_salt, canonical_url, source_url,
               title, item_guid, is_dry_run, is_skipped
        FROM posts
        WHERE identity_salt = ? AND posted_at_unix >= ? AND is_skipped = 0
        ORDER BY posted_at_unix ASC
        """,
        (identity_salt, cutoff),
    )
    return [_row_to_record(row) for row in rows]


def _row_to_record(row: tuple) -> PostRecord:
    return PostRecord(
        posted_at_unix=row[0],
        identity_salt=row[1],
        canonical_url=row[2],
        source_url=row[3],
        title=row[4],
        item_guid=row[5],
        is_dry_run=bool(row[6]),
        is_skipped=bool(row[7]),
    )
