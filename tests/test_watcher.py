"""Tests for the file watcher.

These tests use a real watchdog observer against a temp directory, so they're
mildly platform-sensitive. Each test waits up to a few seconds for the
debounced callback to fire and skips on platforms where watchdog's polling
fallback is too slow to be reliable.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from news_agent.watcher import FileWatcher

WAIT_BUDGET_SECONDS = 5.0
DEBOUNCE_SECONDS = 0.2


def _make_files(tmp_path: Path) -> tuple[Path, Path]:
    feeds = tmp_path / "feeds.opml"
    control = tmp_path / "control.yaml"
    feeds.write_text("<opml version=\"2.0\"/>")
    control.write_text("identities: []\n")
    return feeds, control


def _wait_for(condition, timeout: float = WAIT_BUDGET_SECONDS) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_modifying_feeds_file_fires_feeds_callback(tmp_path):
    feeds, control = _make_files(tmp_path)
    events: list[str] = []
    seen_event = threading.Event()

    def on_change(key: str) -> None:
        events.append(key)
        seen_event.set()

    watcher = FileWatcher(feeds, control, on_change, debounce_seconds=DEBOUNCE_SECONDS)
    watcher.start()
    try:
        time.sleep(0.2)
        feeds.write_text("<opml version=\"2.0\"/><!-- changed -->")
        assert seen_event.wait(WAIT_BUDGET_SECONDS), "callback didn't fire"
    finally:
        watcher.stop()

    assert "feeds" in events


def test_modifying_control_file_fires_control_callback(tmp_path):
    feeds, control = _make_files(tmp_path)
    events: list[str] = []
    seen_event = threading.Event()

    def on_change(key: str) -> None:
        events.append(key)
        seen_event.set()

    watcher = FileWatcher(feeds, control, on_change, debounce_seconds=DEBOUNCE_SECONDS)
    watcher.start()
    try:
        time.sleep(0.2)
        control.write_text("identities: []  # change\n")
        assert seen_event.wait(WAIT_BUDGET_SECONDS), "callback didn't fire"
    finally:
        watcher.stop()

    assert "control" in events


def test_burst_of_writes_is_debounced_to_one_callback(tmp_path):
    feeds, control = _make_files(tmp_path)
    events: list[tuple[str, float]] = []
    lock = threading.Lock()

    def on_change(key: str) -> None:
        with lock:
            events.append((key, time.monotonic()))

    watcher = FileWatcher(feeds, control, on_change, debounce_seconds=DEBOUNCE_SECONDS)
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

    control_events = [e for e in events if e[0] == "control"]
    assert len(control_events) == 1, f"expected 1 debounced callback, got {len(control_events)}"


def test_unrelated_file_in_same_dir_is_ignored(tmp_path):
    feeds, control = _make_files(tmp_path)
    events: list[str] = []

    def on_change(key: str) -> None:
        events.append(key)

    watcher = FileWatcher(feeds, control, on_change, debounce_seconds=DEBOUNCE_SECONDS)
    watcher.start()
    try:
        time.sleep(0.2)
        # Touch a sibling file the watcher should not care about.
        sibling = tmp_path / "unrelated.txt"
        sibling.write_text("noise")
        time.sleep(DEBOUNCE_SECONDS * 3)
    finally:
        watcher.stop()

    assert events == []
