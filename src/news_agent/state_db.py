"""SQLite database for daemon-wide persistent state.

Lives at ``<daemon_dir>/state.sqlite``. The schema is intentionally NOT defined
here yet — tables will be added by feature blocks (dedupe, digest log,
ETag/Last-Modified cache, etc.) as they land. For now this module just opens
or creates the file and hands back a connection with sensible pragmas set.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_DB_FILENAME = "state.sqlite"


def open_state_db(daemon_dir: Path) -> sqlite3.Connection:
    """Open (or create) the daemon's SQLite state DB and return the connection.

    No tables are defined yet. The connection is configured with:
    - ``isolation_level=None`` so callers can manage transactions explicitly,
    - WAL journal mode for concurrent readers + non-blocking writes,
    - foreign keys on (cheap insurance for future schemas).

    The caller is responsible for closing the connection on shutdown.
    """
    db_path = daemon_dir / STATE_DB_FILENAME
    is_new = not db_path.exists()
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if is_new:
        logger.info("created new state database at %s", db_path)
    else:
        logger.info("opened existing state database at %s", db_path)
    return conn
