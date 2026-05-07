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
            hashiverse_post_id  TEXT,
            is_dry_run          INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at_unix)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_canonical ON posts(canonical_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_identity  ON posts(identity_salt)")

    # feed_cache: per-source-URL cached body + conditional-GET headers.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feed_cache (
            source_url      TEXT PRIMARY KEY,
            body            BLOB NOT NULL,
            etag            TEXT,
            last_modified   TEXT,
            fetched_at_unix INTEGER NOT NULL
        )
        """
    )
