"""In-memory runtime state — the parsed-and-validated control file + feeds list.

State is swapped atomically when either input file changes on disk. Readers
that grabbed a snapshot keep using their snapshot until they ask for a fresh
one.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from news_agent.config import ControlConfig
from news_agent.opml_loader import FeedSpec


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Immutable view of the daemon's current loaded state."""

    feeds: tuple[FeedSpec, ...]
    control: ControlConfig


class RuntimeState:
    """A thread-safe holder for the current snapshot.

    Use :meth:`snapshot` to read; use :meth:`swap` from the watcher's reload
    callback to publish a new state. Readers that grabbed a snapshot before the
    swap keep their old snapshot — there's no shared mutable state.
    """

    def __init__(self, initial: RuntimeSnapshot) -> None:
        self._lock = threading.Lock()
        self._snapshot = initial

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def swap(self, new_snapshot: RuntimeSnapshot) -> None:
        with self._lock:
            self._snapshot = new_snapshot
