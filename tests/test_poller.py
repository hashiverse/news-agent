"""Tests for poller — interval respect, prompt shutdown, exception isolation."""

from __future__ import annotations

import threading
import time

import pytest

from news_agent.poller import RemotePoller


def test_poller_calls_fetch_at_interval():
    calls: list[float] = []
    stop = threading.Event()

    def fetch() -> None:
        calls.append(time.monotonic())

    poller = RemotePoller(
        name="test", fetch_fn=fetch, stop_event=stop, interval_seconds=0.1
    )
    poller.start()
    time.sleep(0.5)
    poller.stop(timeout=1.0)

    # Expect ~4 calls in 0.5s with 0.1s interval (initial wait, then 4 fires).
    assert len(calls) >= 3, f"got {len(calls)} calls"
    # Intervals between calls should be roughly the configured interval.
    if len(calls) >= 2:
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        assert all(0.05 < g < 0.3 for g in gaps), gaps


def test_stop_event_stops_thread_quickly():
    stop = threading.Event()
    calls = {"n": 0}

    def fetch() -> None:
        calls["n"] += 1

    poller = RemotePoller(
        name="test", fetch_fn=fetch, stop_event=stop, interval_seconds=10.0
    )
    poller.start()
    time.sleep(0.05)
    started_at = time.monotonic()
    stop.set()
    poller.stop(timeout=2.0)
    elapsed = time.monotonic() - started_at

    # With a 10s interval but the stop event set, shutdown should happen
    # within a fraction of a second — Event.wait() returns immediately on set.
    assert elapsed < 1.0, f"stop took {elapsed:.2f}s"
    # No fetch should have run before stop fired (interval is 10s).
    assert calls["n"] == 0


def test_exception_in_fetch_does_not_kill_thread():
    stop = threading.Event()
    calls = {"n": 0}

    def fetch() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first call always fails")

    poller = RemotePoller(
        name="test", fetch_fn=fetch, stop_event=stop, interval_seconds=0.05
    )
    poller.start()
    time.sleep(0.3)
    poller.stop(timeout=1.0)

    # Despite the exception on the first call, subsequent calls should happen.
    assert calls["n"] >= 3, f"got {calls['n']} calls"


def test_double_start_is_idempotent():
    stop = threading.Event()
    poller = RemotePoller(
        name="test", fetch_fn=lambda: None, stop_event=stop, interval_seconds=10
    )
    poller.start()
    poller.start()  # must not crash, must not spawn a second thread
    poller.stop()


def test_stop_without_start_is_ok():
    stop = threading.Event()
    poller = RemotePoller(
        name="test", fetch_fn=lambda: None, stop_event=stop, interval_seconds=10
    )
    poller.stop()  # nothing to stop; must be benign


def test_zero_interval_is_rejected():
    stop = threading.Event()
    with pytest.raises(ValueError):
        RemotePoller(name="x", fetch_fn=lambda: None, stop_event=stop, interval_seconds=0)
    with pytest.raises(ValueError):
        RemotePoller(name="x", fetch_fn=lambda: None, stop_event=stop, interval_seconds=-1)
