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

from news_agent.config import ControlConfig, IdentityConfig
from news_agent.posts_db import (
    posted_canonical_urls_in_last_24h,
    posts_in_last_24h_for_identity,
)
from news_agent.runner import run_loop
from news_agent.runtime_state import RuntimeSnapshot, RuntimeState
from news_agent.state_db import open_state_db


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class _FakeClient:
    """No-op stand-in for the real HashiverseClient."""

    def __init__(self, client_id: str = "fake-client-id") -> None:
        self.client_id = client_id
        self.posted: list[str] = []

    def post_with_preprocessing(self, text: str) -> None:
        self.posted.append(text)


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
