"""Tests for runner.run_loop — the scheduling-and-posting loop integration.

Drives the loop with a fake clock, fake hashiverse client (no network), and
controlled feed bodies. The loop runs on a background thread; tests trigger
``stop_event`` after each verifiable state change.
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from news_agent import posting
from news_agent.config import ControlConfig, IdentityConfig
from news_agent.posts_db import (
    posted_canonical_urls_in_last_24h,
    posts_in_last_24h_for_identity,
)
from news_agent.picker import PickerCounts
from news_agent.runner import (
    _format_duration,
    _format_local_time,
    _format_picker_counts,
    _interruptible_sleep,
    _truncate_title,
    _wait_until,
    run_loop,
)
from news_agent.runtime_state import RuntimeSnapshot, RuntimeState
from news_agent.state_db import open_state_db
from news_agent.url_preview import UrlPreviewData


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class _FakeClient:
    """No-op stand-in for the real HashiverseClient. Preview fetching now
    happens locally in news_agent.url_preview; the ``stub_url_preview`` fixture
    below patches it to a no-op for these integration tests."""

    def __init__(self, client_id: str = "fake-client-id") -> None:
        self.client_id = client_id
        self.posted: list[str] = []

    def submit_post(self, html_body: str) -> None:
        self.posted.append(html_body)


@pytest.fixture(autouse=True)
def stub_url_preview(monkeypatch):
    """Replace posting.fetch_url_preview with a no-op for all runner tests.

    The runner doesn't care about preview content — it just needs the post
    to be constructed and submitted. Without this, every real-run test would
    hit the network trying to fetch the example.com URLs in test articles.
    """
    monkeypatch.setattr(posting, "fetch_url_preview", lambda url: UrlPreviewData())


class _NoJitterRandom(random.Random):
    """RNG that returns 0 from randint and always picks index 0 from choice.

    Lets the runner integration tests run synchronously without scheduling
    waits — jitter is exercised in test_scheduler with a real Random.
    """

    def randint(self, a: int, b: int) -> int:  # type: ignore[override]
        return 0

    def choice(self, seq):  # type: ignore[override]
        return seq[0]


def _make_rss(*, title: str, url: str, pub_date: str = "Wed, 07 May 2026 12:00:00 GMT") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Smoke feed</title>
    <link>https://example.com/</link>
    <description>x</description>
    <item>
      <title>{title}</title>
      <link>{url}</link>
      <guid>{url}</guid>
      <pubDate>{pub_date}</pubDate>
    </item>
  </channel>
</rss>""".encode("utf-8")


def _identity(salt: str, nickname: str, source_url: str, max_per_day: int = 100) -> IdentityConfig:
    return IdentityConfig(
        salt=salt,
        nickname=nickname,
        status="status",
        max_posts_per_day=max_per_day,
        sources=(source_url,),
    )


@contextmanager
def _running_server(handler_cls: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _make_static_handler(body: bytes) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("ETag", '"v1"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture
def conn(tmp_path):
    daemon_dir = tmp_path / "d"
    daemon_dir.mkdir()
    c = open_state_db(daemon_dir)
    yield c
    c.close()


SALT_A = "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
SALT_B = "c3a7e2f1b9d4a8e6c2f5d1b8e4a7c3f6d2b9e5a8c1f4d7b3e9a6c2f5d8b1e4c7"


def _wait_for(condition, timeout: float = 5.0, *, sleep: float = 0.05) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if condition():
            return True
        time.sleep(sleep)
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_loop_picks_eligible_article_and_records_dry_run(conn):
    """Single identity, single source with one fresh article. Loop posts it (dry-run)."""
    body = _make_rss(
        title="Hello",
        url="https://example.com/hello",
        pub_date=time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
    )
    handler = _make_static_handler(body)
    with _running_server(handler) as base:
        source_url = f"{base}/feed.xml"
        identity = _identity(SALT_A, "Smoker", source_url)
        state = RuntimeState(
            RuntimeSnapshot(control=ControlConfig(identities=(identity,)))
        )
        client = _FakeClient()
        clients: dict[str, object] = {SALT_A: client}
        stop_event = threading.Event()
        rng = _NoJitterRandom()

        def loop_target() -> None:
            run_loop(
                state=state,
                clients=clients,
                conn=conn,
                stop_event=stop_event,
                reload_event=threading.Event(),
                dry_run=True,
                rng=rng,
            )

        thread = threading.Thread(target=loop_target, daemon=True)
        thread.start()

        # The loop should record the dry-run post within a couple of seconds.
        assert _wait_for(
            lambda: posts_in_last_24h_for_identity(conn, SALT_A, int(time.time())),
            timeout=10.0,
        ), "loop did not record a dry-run post"

        stop_event.set()
        thread.join(timeout=5)

    posts = posts_in_last_24h_for_identity(conn, SALT_A, int(time.time()) + 60)
    assert len(posts) == 1
    assert posts[0].is_dry_run is True
    assert posts[0].canonical_url == "https://example.com/hello"
    # Real client.post_with_preprocessing was NOT called in dry-run mode.
    assert client.posted == []


def test_loop_does_not_repost_within_24h(conn):
    """Same identity, same article — loop must not pick it twice in 24h."""
    body = _make_rss(
        title="Once",
        url="https://example.com/once",
        pub_date=time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
    )
    handler = _make_static_handler(body)
    with _running_server(handler) as base:
        source_url = f"{base}/feed.xml"
        identity = _identity(SALT_A, "Smoker", source_url)
        state = RuntimeState(
            RuntimeSnapshot(control=ControlConfig(identities=(identity,)))
        )
        clients: dict[str, object] = {SALT_A: _FakeClient()}
        stop_event = threading.Event()

        thread = threading.Thread(
            target=run_loop,
            kwargs=dict(
                state=state,
                clients=clients,
                conn=conn,
                stop_event=stop_event,
                reload_event=threading.Event(),
                dry_run=True,
                rng=_NoJitterRandom(),
            ),
            daemon=True,
        )
        thread.start()

        assert _wait_for(
            lambda: posts_in_last_24h_for_identity(conn, SALT_A, int(time.time())),
            timeout=10.0,
        )
        # Give the loop a couple more cycles to attempt re-posting.
        time.sleep(2.0)
        stop_event.set()
        thread.join(timeout=5)

    posts = posts_in_last_24h_for_identity(conn, SALT_A, int(time.time()) + 60)
    # Exactly one row — same article wasn't picked again.
    assert len(posts) == 1


def test_cross_identity_dedupe_prevents_double_post_of_same_url(conn):
    """Two identities, same source. The article is posted once (cross-identity dedupe)."""
    body = _make_rss(
        title="Shared",
        url="https://example.com/shared",
        pub_date=time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
    )
    handler = _make_static_handler(body)
    with _running_server(handler) as base:
        source_url = f"{base}/feed.xml"
        ident_a = _identity(SALT_A, "A", source_url)
        ident_b = _identity(SALT_B, "B", source_url)
        state = RuntimeState(
            RuntimeSnapshot(control=ControlConfig(identities=(ident_a, ident_b)))
        )
        clients: dict[str, object] = {
            SALT_A: _FakeClient("a"),
            SALT_B: _FakeClient("b"),
        }
        stop_event = threading.Event()

        thread = threading.Thread(
            target=run_loop,
            kwargs=dict(
                state=state,
                clients=clients,
                conn=conn,
                stop_event=stop_event,
                reload_event=threading.Event(),
                dry_run=True,
                rng=_NoJitterRandom(),
            ),
            daemon=True,
        )
        thread.start()

        assert _wait_for(
            lambda: bool(
                posted_canonical_urls_in_last_24h(conn, int(time.time()))
            ),
            timeout=10.0,
        )
        time.sleep(2.0)
        stop_event.set()
        thread.join(timeout=5)

    urls_posted = posted_canonical_urls_in_last_24h(conn, int(time.time()) + 60)
    # Only one canonical URL across all rows — the shared article.
    assert urls_posted == {"https://example.com/shared"}


def test_loop_stops_promptly_on_stop_event(conn):
    """The loop should exit within a couple of seconds of stop_event.set()."""
    body = _make_rss(
        title="x",
        url="https://example.com/x",
        pub_date=time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
    )
    handler = _make_static_handler(body)
    with _running_server(handler) as base:
        source_url = f"{base}/feed.xml"
        identity = _identity(SALT_A, "A", source_url, max_per_day=1)  # quickly hits cap
        state = RuntimeState(
            RuntimeSnapshot(control=ControlConfig(identities=(identity,)))
        )
        clients: dict[str, object] = {SALT_A: _FakeClient()}
        stop_event = threading.Event()

        thread = threading.Thread(
            target=run_loop,
            kwargs=dict(
                state=state,
                clients=clients,
                conn=conn,
                stop_event=stop_event,
                reload_event=threading.Event(),
                dry_run=True,
                rng=_NoJitterRandom(),
            ),
            daemon=True,
        )
        thread.start()
        time.sleep(0.5)
        started_stop = time.monotonic()
        stop_event.set()
        thread.join(timeout=3)
        elapsed = time.monotonic() - started_stop

    assert not thread.is_alive()
    assert elapsed < 2.5, f"shutdown took {elapsed:.1f}s"


def test_real_run_calls_client_post(conn):
    """When dry_run=False, the fake client.post_with_preprocessing IS called."""
    body = _make_rss(
        title="Real",
        url="https://example.com/real",
        pub_date=time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
    )
    handler = _make_static_handler(body)
    with _running_server(handler) as base:
        source_url = f"{base}/feed.xml"
        identity = _identity(SALT_A, "A", source_url)
        state = RuntimeState(
            RuntimeSnapshot(control=ControlConfig(identities=(identity,)))
        )
        client = _FakeClient()
        clients: dict[str, object] = {SALT_A: client}
        stop_event = threading.Event()

        thread = threading.Thread(
            target=run_loop,
            kwargs=dict(
                state=state,
                clients=clients,
                conn=conn,
                stop_event=stop_event,
                reload_event=threading.Event(),
                dry_run=False,
                rng=_NoJitterRandom(),
            ),
            daemon=True,
        )
        thread.start()
        assert _wait_for(lambda: bool(client.posted), timeout=10.0)
        stop_event.set()
        thread.join(timeout=5)

    assert len(client.posted) == 1
    assert "Real" in client.posted[0]
    posts = posts_in_last_24h_for_identity(conn, SALT_A, int(time.time()) + 60)
    assert posts[0].is_dry_run is False


# ---------------------------------------------------------------------------
# Duration / time formatters
# ---------------------------------------------------------------------------


def test_format_duration_seconds_only():
    assert _format_duration(0) == "0s"
    assert _format_duration(7) == "7s"
    assert _format_duration(59) == "59s"


def test_format_duration_minutes_and_seconds():
    assert _format_duration(60) == "1m00s"
    assert _format_duration(125) == "2m05s"
    assert _format_duration(3599) == "59m59s"


def test_format_duration_hours_and_minutes():
    assert _format_duration(3600) == "1h00m"
    assert _format_duration(3 * 3600 + 7 * 60 + 30) == "3h07m"
    assert _format_duration(86399) == "23h59m"


def test_format_duration_days_and_hours():
    assert _format_duration(86400) == "1d00h"
    assert _format_duration(86400 + 5 * 3600 + 30 * 60) == "1d05h"


def test_format_duration_clamps_negative_to_zero():
    """Schedules can occasionally slip past 'now' between compute + log."""
    assert _format_duration(-1) == "0s"
    assert _format_duration(-10000) == "0s"


def test_format_local_time_round_trips_an_hms_string():
    # Just confirm the format is HH:MM:SS — the actual value is timezone-dependent.
    formatted = _format_local_time(int(time.time()))
    assert len(formatted) == 8
    assert formatted[2] == ":" and formatted[5] == ":"


def test_truncate_title_passes_short_titles_through():
    assert _truncate_title("short") == "short"
    assert _truncate_title("  spaces  ") == "spaces"


def test_truncate_title_clips_long_titles_with_ellipsis():
    long_title = "x" * 100
    out = _truncate_title(long_title, max_len=20)
    assert len(out) == 20
    assert out.endswith("…")


# ---------------------------------------------------------------------------
# "Nothing eligible" log line announcing the next-scheduled identity
# ---------------------------------------------------------------------------


def _make_empty_rss() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Empty</title>
    <link>https://example.com/</link>
    <description>nothing</description>
  </channel>
</rss>"""


def test_loop_logs_next_scheduled_identity_when_nothing_eligible(conn, caplog):
    """Empty feed → no eligible article → log announces the soonest scheduled identity."""
    handler = _make_static_handler(_make_empty_rss())
    with _running_server(handler) as base:
        source_url = f"{base}/feed.xml"
        identity = _identity(SALT_A, "Quietist", source_url)
        state = RuntimeState(
            RuntimeSnapshot(control=ControlConfig(identities=(identity,)))
        )
        clients: dict[str, object] = {SALT_A: _FakeClient()}
        stop_event = threading.Event()

        thread = threading.Thread(
            target=run_loop,
            kwargs=dict(
                state=state,
                clients=clients,
                conn=conn,
                stop_event=stop_event,
                reload_event=threading.Event(),
                dry_run=True,
                rng=_NoJitterRandom(),
            ),
            daemon=True,
        )
        with caplog.at_level("INFO", logger="news_agent.runner"):
            thread.start()
            captured = _wait_for(
                lambda: any(
                    "nothing eligible right now" in r.getMessage()
                    for r in caplog.records
                ),
                timeout=10.0,
            )
            stop_event.set()
            thread.join(timeout=5)

    assert captured, "expected 'nothing eligible right now' log line"
    matching = [
        r.getMessage() for r in caplog.records if "nothing eligible right now" in r.getMessage()
    ]
    msg = matching[0]
    assert "Quietist" in msg
    assert "next scheduled is" in msg
    # Format includes a time of day and a duration.
    assert " at " in msg and " in " in msg


# ---------------------------------------------------------------------------
# Per-identity picker rejection breakdown — when the picker returns nothing,
# the runner logs a one-line summary so the operator can see *why* nothing
# was eligible.


def test_format_picker_counts_omits_zero_buckets():
    counts = PickerCounts(
        total_candidates=10,
        rejected_dedupe=7,
        rejected_no_publish_date=0,
        rejected_stale=2,
        rejected_future_dated=0,
        rejected_keywords_required=1,
        rejected_keywords_optional=0,
        eligible=0,
    )
    line = _format_picker_counts(counts)
    # Non-zero buckets appear in source order.
    assert "10 candidates → 7 dedupe, 2 stale, 1 missing required keyword → 0 eligible" == line


def test_format_picker_counts_all_eligible():
    counts = PickerCounts(
        total_candidates=3,
        rejected_dedupe=0,
        rejected_no_publish_date=0,
        rejected_stale=0,
        rejected_future_dated=0,
        rejected_keywords_required=0,
        rejected_keywords_optional=0,
        eligible=3,
    )
    line = _format_picker_counts(counts)
    assert line == "3 candidates → no rejections → 3 eligible"


def test_format_picker_counts_empty_pool():
    counts = PickerCounts(
        total_candidates=0,
        rejected_dedupe=0,
        rejected_no_publish_date=0,
        rejected_stale=0,
        rejected_future_dated=0,
        rejected_keywords_required=0,
        rejected_keywords_optional=0,
        eligible=0,
    )
    assert _format_picker_counts(counts) == "0 candidates → no rejections → 0 eligible"


def test_logs_picker_breakdown_when_nothing_eligible(conn, caplog):
    """After the loop posts an article, the next iteration's picker finds
    the same article already in the dedupe set and rejects it. The runner
    must log the per-reason breakdown at INFO so the operator can see why."""
    body = _make_rss(
        title="Once",
        url="https://example.com/once",
        pub_date=time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
    )
    handler = _make_static_handler(body)
    with _running_server(handler) as base:
        source_url = f"{base}/feed.xml"
        identity = _identity(SALT_A, "Dedupe canary", source_url)
        state = RuntimeState(
            RuntimeSnapshot(control=ControlConfig(identities=(identity,)))
        )
        clients: dict[str, object] = {SALT_A: _FakeClient()}
        stop_event = threading.Event()

        thread = threading.Thread(
            target=run_loop,
            kwargs=dict(
                state=state,
                clients=clients,
                conn=conn,
                stop_event=stop_event,
                reload_event=threading.Event(),
                dry_run=True,
                rng=_NoJitterRandom(),
            ),
            daemon=True,
        )
        with caplog.at_level("INFO", logger="news_agent.runner"):
            thread.start()
            # Wait for the breakdown line that fires once the article is
            # in the dedupe set. The loop posts on iteration 1, hits the
            # dedupe rejection on iteration 2 — both happen within ~1s
            # of the test starting.
            captured = _wait_for(
                lambda: any(
                    "no eligible article for" in r.getMessage()
                    and "dedupe" in r.getMessage()
                    for r in caplog.records
                ),
                timeout=10.0,
            )
            stop_event.set()
            thread.join(timeout=5)

    assert captured, "expected breakdown log line citing dedupe"
    breakdown = next(
        r.getMessage()
        for r in caplog.records
        if "no eligible article for" in r.getMessage()
    )
    assert "Dedupe canary" in breakdown
    assert "1 dedupe" in breakdown
    assert "→ 0 eligible" in breakdown
    # Other buckets should not appear because they had zero rejections.
    assert "stale" not in breakdown
    assert "no-date" not in breakdown


# ---------------------------------------------------------------------------
# Wait helpers wake on reload_event so a config reload can cancel an in-flight
# scheduled-post wait and force the runner to re-evaluate from scratch.


def test_wait_until_returns_false_when_reload_event_already_set():
    stop_event = threading.Event()
    reload_event = threading.Event()
    reload_event.set()
    # Target is 60s in the future — without reload_event, this would block.
    target = int(time.time()) + 60
    started = time.monotonic()
    result = _wait_until(target, stop_event, reload_event)
    elapsed = time.monotonic() - started
    assert result is False
    assert elapsed < 1.0, f"_wait_until didn't return promptly: {elapsed:.2f}s"


def test_wait_until_returns_false_when_reload_event_set_mid_wait():
    stop_event = threading.Event()
    reload_event = threading.Event()
    target = int(time.time()) + 60

    def fire_reload() -> None:
        time.sleep(0.3)
        reload_event.set()

    threading.Thread(target=fire_reload, daemon=True).start()
    started = time.monotonic()
    result = _wait_until(target, stop_event, reload_event)
    elapsed = time.monotonic() - started
    assert result is False
    assert elapsed < 5.0, f"_wait_until didn't wake on reload: {elapsed:.2f}s"


def test_interruptible_sleep_returns_true_when_reload_event_set():
    stop_event = threading.Event()
    reload_event = threading.Event()

    def fire_reload() -> None:
        time.sleep(0.3)
        reload_event.set()

    threading.Thread(target=fire_reload, daemon=True).start()
    started = time.monotonic()
    result = _interruptible_sleep(stop_event, reload_event, total_seconds=60.0)
    elapsed = time.monotonic() - started
    assert result is True
    assert elapsed < 5.0, f"_interruptible_sleep didn't wake on reload: {elapsed:.2f}s"


def test_loop_recomputes_after_reload(conn):
    """After reload_event fires during a scheduled wait, the loop re-iterates
    and posts using the new identity set."""
    body = _make_rss(
        title="Reloaded",
        url="https://example.com/reloaded",
        pub_date=time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
    )
    handler = _make_static_handler(body)
    with _running_server(handler) as base:
        source_url = f"{base}/feed.xml"

        # Start with NO identities so the loop sleeps in the no-eligible branch.
        state = RuntimeState(RuntimeSnapshot(control=ControlConfig(identities=())))
        clients: dict[str, object] = {}
        stop_event = threading.Event()
        reload_event = threading.Event()

        def loop_target() -> None:
            run_loop(
                state=state,
                clients=clients,
                conn=conn,
                stop_event=stop_event,
                reload_event=reload_event,
                dry_run=True,
                rng=_NoJitterRandom(),
            )

        thread = threading.Thread(target=loop_target, daemon=True)
        thread.start()

        # Let the loop enter its no-eligible sleep.
        time.sleep(0.5)
        assert posts_in_last_24h_for_identity(conn, SALT_A, int(time.time())) == []

        # Mid-flight: swap in a fresh state with one identity, then ring the bell.
        identity = _identity(SALT_A, "Loaded", source_url)
        state.swap(RuntimeSnapshot(control=ControlConfig(identities=(identity,))))
        clients[SALT_A] = _FakeClient()
        reload_event.set()

        # The loop should wake within ~1s, recompute, and post the article.
        assert _wait_for(
            lambda: posts_in_last_24h_for_identity(conn, SALT_A, int(time.time())),
            timeout=5.0,
        ), "loop did not post after reload"

        stop_event.set()
        thread.join(timeout=5)
