"""Tests for the file watcher.

These tests use a real watchdog observer against a temp directory, so they're
mildly platform-sensitive. Each test waits up to a few seconds for the
debounced callback to fire.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from news_agent.watcher import FileWatcher

WAIT_BUDGET_SECONDS = 5.0
DEBOUNCE_SECONDS = 0.2


def _make_control_file(tmp_path: Path) -> Path:
    control = tmp_path / "control.yaml"
    control.write_text("identities: []\n")
    return control


def _wait_for(condition, timeout: float = WAIT_BUDGET_SECONDS) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_modifying_control_file_fires_callback(tmp_path):
    control = _make_control_file(tmp_path)
    fired = threading.Event()

    def on_change() -> None:
        fired.set()

    watcher = FileWatcher(control, on_change, debounce_seconds=DEBOUNCE_SECONDS)
    watcher.start()
    try:
        time.sleep(0.2)
        control.write_text("identities: []  # change\n")
        assert fired.wait(WAIT_BUDGET_SECONDS), "callback didn't fire"
    finally:
        watcher.stop()


def test_burst_of_writes_is_debounced_to_one_callback(tmp_path):
    control = _make_control_file(tmp_path)
    events: list[float] = []
    lock = threading.Lock()

    def on_change() -> None:
        with lock:
            events.append(time.monotonic())

    watcher = FileWatcher(control, on_change, debounce_seconds=DEBOUNCE_SECONDS)
    watcher.start()
    try:
        time.sleep(0.2)
        for i in range(5):
            control.write_text(f"identities: []  # change {i}\n")
            time.sleep(0.02)  # tighter than the debounce window
        # Wait for at least one event, then a quiet period to confirm coalescing.
        _wait_for(lambda: len(events) >= 1)
        time.sleep(DEBOUNCE_SECONDS * 3)
    finally:
        watcher.stop()

    assert len(events) == 1, f"expected 1 debounced callback, got {len(events)}"


def test_unrelated_file_in_same_dir_is_ignored(tmp_path):
    control = _make_control_file(tmp_path)
    fired = threading.Event()

    def on_change() -> None:
        fired.set()

    watcher = FileWatcher(control, on_change, debounce_seconds=DEBOUNCE_SECONDS)
    watcher.start()
    try:
        time.sleep(0.2)
        # Touch a sibling file the watcher should not care about.
        sibling = tmp_path / "unrelated.txt"
        sibling.write_text("noise")
        time.sleep(DEBOUNCE_SECONDS * 3)
    finally:
        watcher.stop()

    assert not fired.is_set()


def test_callback_exception_does_not_kill_watcher(tmp_path):
    control = _make_control_file(tmp_path)
    calls = {"n": 0}
    lock = threading.Lock()

    def on_change() -> None:
        with lock:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first call always fails")

    watcher = FileWatcher(control, on_change, debounce_seconds=DEBOUNCE_SECONDS)
    watcher.start()
    try:
        time.sleep(0.2)
        for i in range(3):
            control.write_text(f"identities: []  # change {i}\n")
            time.sleep(DEBOUNCE_SECONDS * 2)
    finally:
        watcher.stop()

    # Despite the exception on the first call, subsequent edits still fire.
    assert calls["n"] >= 2, f"got {calls['n']} calls"
