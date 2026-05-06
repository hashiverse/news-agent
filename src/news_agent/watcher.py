"""watchdog wrapper that fires a debounced reload callback when either of two
watched files changes on disk.

Each file is identified by a key (``"feeds"`` or ``"control"``) so the callback
can decide what to do per file. Multiple events that arrive within
``debounce_seconds`` of each other are coalesced into a single callback per key.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_SECONDS = 1.0


class _PerFileHandler(FileSystemEventHandler):
    """Routes events for one specific file to a debounced callback."""

    def __init__(
        self,
        watched_path: Path,
        key: str,
        callback: Callable[[str], None],
        debounce_seconds: float,
    ) -> None:
        self._watched_path = watched_path.resolve()
        self._key = key
        self._callback = callback
        self._debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._pending_timer: threading.Timer | None = None

    def _matches(self, event_src: str) -> bool:
        try:
            return Path(event_src).resolve() == self._watched_path
        except (OSError, ValueError):
            return False

    def _schedule(self) -> None:
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
            timer = threading.Timer(self._debounce_seconds, self._fire)
            timer.daemon = True
            self._pending_timer = timer
            timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._pending_timer = None
        try:
            self._callback(self._key)
        except Exception:  # noqa: BLE001 — keep the watcher thread alive.
            logger.exception("reload callback for %r raised", self._key)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._matches(event.src_path):
            self._schedule()

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if self._matches(event.src_path):
            self._schedule()

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        # Atomic-rename editors (vim, etc.) replace the file via a rename.
        # The destination becomes the new file; treat that as a modification.
        dest = getattr(event, "dest_path", None)
        if dest and self._matches(dest):
            self._schedule()


class FileWatcher:
    """Watches the feeds and control files; fires a debounced reload callback.

    Usage::

        watcher = FileWatcher(feeds_path, control_path, on_change)
        watcher.start()
        ...
        watcher.stop()

    The callback is invoked on a background watchdog thread with the key of the
    file that changed (``"feeds"`` or ``"control"``).
    """

    def __init__(
        self,
        feeds_path: Path,
        control_path: Path,
        on_change: Callable[[str], None],
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._on_change = on_change
        self._observer = Observer()

        feeds_handler = _PerFileHandler(
            feeds_path, "feeds", on_change, debounce_seconds
        )
        control_handler = _PerFileHandler(
            control_path, "control", on_change, debounce_seconds
        )

        # Watchdog watches directories; we filter to specific files inside the handler.
        self._observer.schedule(
            feeds_handler, str(feeds_path.resolve().parent), recursive=False
        )
        if control_path.resolve().parent != feeds_path.resolve().parent:
            self._observer.schedule(
                control_handler, str(control_path.resolve().parent), recursive=False
            )
        else:
            self._observer.schedule(
                control_handler, str(control_path.resolve().parent), recursive=False
            )

    def start(self) -> None:
        self._observer.start()

    def stop(self, timeout: float | None = 2.0) -> None:
        self._observer.stop()
        self._observer.join(timeout=timeout)
