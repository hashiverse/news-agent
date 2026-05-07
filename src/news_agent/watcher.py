"""watchdog wrapper that fires a debounced reload callback when the control
file changes on disk.

Multiple events that arrive within ``debounce_seconds`` of each other are
coalesced into a single callback.
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
        callback: Callable[[], None],
        debounce_seconds: float,
    ) -> None:
        self._watched_path = watched_path.resolve()
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
            self._callback()
        except Exception:  # noqa: BLE001 — keep the watcher thread alive.
            logger.exception("reload callback raised")

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
    """Watches one file; fires a debounced reload callback on every change.

    Usage::

        watcher = FileWatcher(control_path, on_change)
        watcher.start()
        ...
        watcher.stop()
    """

    def __init__(
        self,
        path: Path,
        on_change: Callable[[], None],
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._observer = Observer()
        handler = _PerFileHandler(path, on_change, debounce_seconds)
        # Watchdog watches directories; the handler filters to the specific file.
        self._observer.schedule(
            handler, str(path.resolve().parent), recursive=False
        )

    def start(self) -> None:
        self._observer.start()

    def stop(self, timeout: float | None = 2.0) -> None:
        self._observer.stop()
        self._observer.join(timeout=timeout)
