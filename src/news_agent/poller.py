"""Background polling thread for one remote URL.

A :class:`RemotePoller` wraps a daemon thread that calls a fetch function on a
fixed interval. The first fetch is the operator's responsibility (the daemon
fetches at startup before this poller is even started). Once running, the
poller waits ``interval_seconds``, fetches, repeats — and stops promptly when
the shared :class:`threading.Event` is set.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)


class RemotePoller:
    """A daemon-thread poller that calls ``fetch_fn`` on a schedule."""

    def __init__(
        self,
        name: str,
        fetch_fn: Callable[[], object],
        stop_event: threading.Event,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._name = name
        self._fetch_fn = fetch_fn
        self._stop_event = stop_event
        self._interval = interval_seconds
        self._thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        return self._name

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"news-agent-poller-{self._name}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "poller %r started, interval=%.1fs", self._name, self._interval
        )

    def _run(self) -> None:
        # The initial fetch happens at daemon startup; we sleep first so we
        # don't double-fetch immediately.
        while not self._stop_event.wait(timeout=self._interval):
            try:
                self._fetch_fn()
            except Exception:  # noqa: BLE001 — keep the thread alive on any fault.
                logger.exception("poller %r raised, continuing", self._name)
        logger.info("poller %r stopped", self._name)

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        self._thread = None
