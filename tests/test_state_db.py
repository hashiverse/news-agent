"""Tests for state_db — SQLite open/create."""

from __future__ import annotations

import sqlite3

from news_agent.state_db import STATE_DB_FILENAME, open_state_db


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
