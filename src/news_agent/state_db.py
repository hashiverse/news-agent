"""SQLite database for daemon-wide persistent state.

Lives at ``<daemon_dir>/state.sqlite``. Schema bootstrap runs every time
:func:`open_state_db` is called — each table is declared with
``CREATE TABLE IF NOT EXISTS``, so adding new tables in future feature blocks
is just a matter of editing :func:`_initialize_schema`. No migration system
yet; when we change a *column* we'll add one.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_DB_FILENAME = "state.sqlite"


def open_state_db(daemon_dir: Path) -> sqlite3.Connection:
    """Open (or create) the daemon's SQLite state DB and return the connection.

    The connection is configured with:
    - ``isolation_level=None`` so callers can manage transactions explicitly,
    - WAL journal mode for concurrent readers + non-blocking writes,
    - foreign keys on (cheap insurance for future schemas).

    Schema bootstrap is idempotent: every known table is declared with
    ``CREATE TABLE IF NOT EXISTS``. The caller is responsible for closing the
    connection on shutdown.
    """
    db_path = daemon_dir / STATE_DB_FILENAME
    is_new = not db_path.exists()
    # check_same_thread=False so the connection can be created on the main
    # thread (during startup) and used by the scheduling loop on its own
    # thread. We discipline ourselves to single-writer (the runner is the
    # only writer) — SQLite handles concurrent reads under WAL just fine.
    conn = sqlite3.connect(
        str(db_path), isolation_level=None, check_same_thread=False
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _initialize_schema(conn)
    if is_new:
        logger.info("created new state database at %s", db_path)
    else:
        logger.info("opened existing state database at %s", db_path)
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema bootstrap. Adds tables that don't yet exist."""
    # posts: history of every post the daemon has made (or would have made,
    # in dry-run). Cross-identity dedupe by canonical_url uses this.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            posted_at_unix      INTEGER NOT NULL,
            identity_salt       TEXT NOT NULL,
            canonical_url       TEXT NOT NULL,
            source_url          TEXT NOT NULL,
            title               TEXT NOT NULL,
            item_guid           TEXT,
            is_dry_run          INTEGER NOT NULL,
            is_skipped          INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at_unix)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_canonical ON posts(canonical_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_identity  ON posts(identity_salt)")

    # Migration: legacy databases predate is_skipped. Default 0 keeps every
    # historical row in the "actually posted" set, which is what they were.
    existing_posts_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(posts)")
    }
    if "is_skipped" not in existing_posts_cols:
        conn.execute(
            "ALTER TABLE posts ADD COLUMN is_skipped "
            "INTEGER NOT NULL DEFAULT 0"
        )

    # feed_cache: per-source-URL cached body + conditional-GET headers.
    # `cache_valid_until_unix` is the moment past which the cache is considered
    # stale and the fetcher must revalidate. Each cache write picks a random
    # value in `[now+20min, now+40min]` so feeds fetched together (e.g. at
    # daemon startup) don't all expire at the same moment 30 minutes later —
    # avoids a synchronized re-fetch storm against shared upstreams.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feed_cache (
            source_url             TEXT PRIMARY KEY,
            body                   BLOB NOT NULL,
            etag                   TEXT,
            last_modified          TEXT,
            fetched_at_unix        INTEGER NOT NULL,
            cache_valid_until_unix INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    # Migration: legacy databases predate cache_valid_until_unix. Add it now.
    # SQLite ALTER TABLE ADD COLUMN with DEFAULT works on populated tables;
    # default 0 means "already expired" so the next access revalidates and
    # stamps a real future timestamp.
    existing_feed_cache_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(feed_cache)")
    }
    if "cache_valid_until_unix" not in existing_feed_cache_cols:
        conn.execute(
            "ALTER TABLE feed_cache ADD COLUMN cache_valid_until_unix "
            "INTEGER NOT NULL DEFAULT 0"
        )
