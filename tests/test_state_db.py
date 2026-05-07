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


def test_no_tables_yet(tmp_path):
    """Schema is intentionally empty for now — verify there are no tables yet."""
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    conn = open_state_db(daemon_dir)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert rows == []
    finally:
        conn.close()
