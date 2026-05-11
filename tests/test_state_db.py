"""Tests for state_db — SQLite open/create."""

from __future__ import annotations

import sqlite3

from news_agent.state_db import STATE_DB_FILENAME, open_state_db
from news_agent.feed_cache_db import get_cached, save_cache


def test_open_creates_file_when_missing(tmp_path):
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    db_path = daemon_dir / STATE_DB_FILENAME
    assert not db_path.exists()

    conn = open_state_db(daemon_dir)
    try:
        assert db_path.exists()
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()


def test_open_is_idempotent(tmp_path):
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    conn1 = open_state_db(daemon_dir)
    conn1.close()
    # File now exists; opening again must not corrupt anything.
    conn2 = open_state_db(daemon_dir)
    try:
        # The connection works.
        result = conn2.execute("SELECT 1").fetchone()
        assert result == (1,)
    finally:
        conn2.close()


def test_journal_mode_is_wal(tmp_path):
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    conn = open_state_db(daemon_dir)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_known_tables_exist_after_open(tmp_path):
    """Schema bootstrap creates the known tables; idempotent on re-open."""
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    conn = open_state_db(daemon_dir)
    try:
        rows = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert "posts" in rows
        assert "feed_cache" in rows
    finally:
        conn.close()

    # Re-open — schema bootstrap must be idempotent.
    conn2 = open_state_db(daemon_dir)
    try:
        rows2 = {
            row[0]
            for row in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert rows == rows2
    finally:
        conn2.close()


def test_existing_feed_cache_table_gets_migrated(tmp_path):
    """A pre-existing feed_cache table missing `cache_valid_until_unix` is
    transparently migrated. Existing rows survive with cache_valid_until_unix=0
    (= already expired, so the next access revalidates and stamps a real value)."""
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    db_path = daemon_dir / STATE_DB_FILENAME

    # Hand-roll a legacy schema (the one before the column was added) and
    # populate it with a row.
    legacy = sqlite3.connect(str(db_path))
    legacy.execute(
        """
        CREATE TABLE feed_cache (
            source_url      TEXT PRIMARY KEY,
            body            BLOB NOT NULL,
            etag            TEXT,
            last_modified   TEXT,
            fetched_at_unix INTEGER NOT NULL
        )
        """
    )
    legacy.execute(
        "INSERT INTO feed_cache (source_url, body, etag, last_modified, "
        "fetched_at_unix) VALUES (?, ?, ?, ?, ?)",
        ("https://legacy/feed.xml", b"<rss>legacy</rss>", '"x"', None, 100),
    )
    legacy.commit()
    legacy.close()

    # Reopen via open_state_db — migration runs.
    conn = open_state_db(daemon_dir)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(feed_cache)")}
        assert "cache_valid_until_unix" in cols

        cached = get_cached(conn, "https://legacy/feed.xml")
        assert cached is not None
        assert cached.body == b"<rss>legacy</rss>"
        assert cached.fetched_at_unix == 100
        # Default 0 — i.e. "already expired" — so the next fetch revalidates.
        assert cached.cache_valid_until_unix == 0

        # And the column is fully usable: write a new row with an explicit value.
        save_cache(
            conn,
            source_url="https://fresh/feed.xml",
            body=b"<rss/>",
            etag=None,
            last_modified=None,
            fetched_at_unix=200,
            cache_valid_until_unix=2000,
        )
        fresh = get_cached(conn, "https://fresh/feed.xml")
        assert fresh.cache_valid_until_unix == 2000
    finally:
        conn.close()
