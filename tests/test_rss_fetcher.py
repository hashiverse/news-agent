"""Tests for rss_fetcher — conditional GET with SQLite-backed cache."""

from __future__ import annotations

import random
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from news_agent.feed_cache_db import get_cached, save_cache
from news_agent.rss_fetcher import (
    CACHE_VALIDITY_MAX_SECONDS,
    CACHE_VALIDITY_MIN_SECONDS,
    FeedFetchError,
    fetch_feed_body,
)
from news_agent.state_db import open_state_db


@pytest.fixture
def conn(tmp_path):
    daemon_dir = tmp_path / "d"
    daemon_dir.mkdir()
    c = open_state_db(daemon_dir)
    yield c
    c.close()


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


def _make_handler(
    body: bytes = b"<rss/>",
    etag: str | None = '"v1"',
    last_modified: str | None = None,
    error_status: int | None = None,
) -> type[BaseHTTPRequestHandler]:
    received_headers: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            received_headers.append({k: v for k, v in self.headers.items()})
            if error_status is not None:
                self.send_response(error_status)
                self.end_headers()
                return
            inm = self.headers.get("If-None-Match")
            ims = self.headers.get("If-Modified-Since")
            if (etag and inm == etag) or (last_modified and ims == last_modified):
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            if etag:
                self.send_header("ETag", etag)
            if last_modified:
                self.send_header("Last-Modified", last_modified)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    Handler.received_headers = received_headers  # type: ignore[attr-defined]
    return Handler


# ---------------------------------------------------------------------------
# Conditional-GET / 200 / 304 / error-fallback paths
#
# Tests that need to bypass the per-row freshness short-circuit pre-seed the
# cache row with `cache_valid_until_unix=<past timestamp>` so the fetcher
# proceeds to issue a real HTTP request. Tests that exercise the short-circuit
# pre-seed it with a future timestamp.


def test_first_fetch_writes_cache(conn):
    handler = _make_handler(body=b"<rss>v1</rss>", etag='"abc"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        body = fetch_feed_body(url, conn, now_unix=1000, rng=random.Random(0))
    assert body == b"<rss>v1</rss>"
    cached = get_cached(conn, url)
    assert cached is not None
    assert cached.body == b"<rss>v1</rss>"
    assert cached.etag == '"abc"'
    assert cached.fetched_at_unix == 1000


def test_second_fetch_with_etag_returns_cached_body_and_bumps_fetched_at(conn):
    handler = _make_handler(body=b"<rss>v1</rss>", etag='"abc"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        # First fetch writes a randomized future expiry. To force the second
        # fetch through the conditional-GET path we'd otherwise have to wait;
        # instead, after the first fetch, manually expire the cache row by
        # rewriting it with a past `cache_valid_until_unix`.
        fetch_feed_body(url, conn, now_unix=1000, rng=random.Random(0))
        cached = get_cached(conn, url)
        save_cache(
            conn,
            source_url=url,
            body=cached.body,
            etag=cached.etag,
            last_modified=cached.last_modified,
            fetched_at_unix=cached.fetched_at_unix,
            cache_valid_until_unix=0,  # already expired
        )
        body = fetch_feed_body(url, conn, now_unix=2000, rng=random.Random(0))

    assert body == b"<rss>v1</rss>"
    # Second request should have included the ETag.
    assert handler.received_headers[1].get("If-None-Match") == '"abc"'  # type: ignore[attr-defined]
    cached = get_cached(conn, url)
    assert cached.fetched_at_unix == 2000


def test_uses_last_modified_when_no_etag(conn):
    handler = _make_handler(
        body=b"<rss/>",
        etag=None,
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
    )
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        fetch_feed_body(url, conn, now_unix=1000, rng=random.Random(0))
        cached = get_cached(conn, url)
        save_cache(
            conn,
            source_url=url,
            body=cached.body,
            etag=cached.etag,
            last_modified=cached.last_modified,
            fetched_at_unix=cached.fetched_at_unix,
            cache_valid_until_unix=0,
        )
        fetch_feed_body(url, conn, now_unix=2000, rng=random.Random(0))

    ims = handler.received_headers[1].get("If-Modified-Since")  # type: ignore[attr-defined]
    assert ims == "Wed, 01 Jan 2025 00:00:00 GMT"


def test_500_with_existing_cache_returns_stale_body(conn):
    """Server starts returning 500 — fetcher returns the cached body."""
    handler = _make_handler(error_status=500)
    # Run via a real localhost server so urllib actually connects.
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        # Seed cache with an already-expired validity window so the fetcher
        # bypasses the short-circuit and exercises the error-fallback path.
        save_cache(
            conn,
            source_url=url,
            body=b"<rss>cached</rss>",
            etag='"old"',
            last_modified=None,
            fetched_at_unix=500,
            cache_valid_until_unix=0,
        )
        body = fetch_feed_body(url, conn, now_unix=1000, rng=random.Random(0))

    assert body == b"<rss>cached</rss>"
    # Cache content untouched (no save_cache, no update_fetched_at on this path).
    cached = get_cached(conn, url)
    assert cached.fetched_at_unix == 500
    assert cached.cache_valid_until_unix == 0


def test_500_without_cache_raises(conn):
    handler = _make_handler(error_status=500)
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        with pytest.raises(FeedFetchError):
            fetch_feed_body(url, conn, now_unix=1000, rng=random.Random(0))


def test_unreachable_host_with_no_cache_raises(conn):
    # Port 1 is reserved and won't accept connections.
    with pytest.raises(FeedFetchError):
        fetch_feed_body(
            "http://127.0.0.1:1/x", conn, timeout=1, now_unix=1000,
            rng=random.Random(0),
        )


def test_unreachable_host_with_cache_returns_stale(conn):
    url = "http://127.0.0.1:1/x"
    save_cache(
        conn,
        source_url=url,
        body=b"stale",
        etag=None,
        last_modified=None,
        fetched_at_unix=100,
        cache_valid_until_unix=0,  # expired → triggers network attempt
    )
    body = fetch_feed_body(
        url, conn, timeout=1, now_unix=1000, rng=random.Random(0),
    )
    assert body == b"stale"


def test_cache_replaces_on_200(conn):
    """200 response replaces the cached body even when one already exists."""
    handler = _make_handler(body=b"<rss>NEW</rss>", etag='"new"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        # Prime with an expired entry (forces revalidation) and a different
        # ETag so the server's stored "new" doesn't match → 200 path.
        save_cache(
            conn,
            source_url=url,
            body=b"<rss>OLD</rss>",
            etag='"old"',
            last_modified=None,
            fetched_at_unix=500,
            cache_valid_until_unix=0,
        )
        body = fetch_feed_body(url, conn, now_unix=1000, rng=random.Random(0))

    # Server's etag is "new", request sent If-None-Match: "old", so server
    # returns 200 with the new content. Fetcher updates cache with new body.
    assert body == b"<rss>NEW</rss>"
    cached = get_cached(conn, url)
    assert cached.body == b"<rss>NEW</rss>"
    assert cached.etag == '"new"'


# ---------------------------------------------------------------------------
# Operator-visible logging — both the network-fetch path and the cache-hit
# path must log at INFO so the operator can tell whether the cache is
# actually being used.


def test_logs_downloaded_marker_on_200_response(conn, caplog):
    handler = _make_handler(body=b"<rss>DATA</rss>", etag='"v1"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        with caplog.at_level("INFO", logger="news_agent.rss_fetcher"):
            fetch_feed_body(url, conn, now_unix=1000, rng=random.Random(0))
    matching = [r.getMessage() for r in caplog.records if "fetched" in r.getMessage()]
    assert len(matching) == 1
    assert "downloaded" in matching[0]
    assert "from cache" not in matching[0]


def test_logs_from_cache_marker_on_304_response(conn, caplog):
    """Regression: a cache hit must be visible at INFO level — previously
    the 304 path logged at DEBUG, making the cache appear unused."""
    handler = _make_handler(body=b"<rss/>", etag='"v1"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        save_cache(
            conn,
            source_url=url,
            body=b"<rss>CACHED</rss>",
            etag='"v1"',
            last_modified=None,
            fetched_at_unix=500,
            cache_valid_until_unix=0,  # force revalidation
        )
        with caplog.at_level("INFO", logger="news_agent.rss_fetcher"):
            body = fetch_feed_body(url, conn, now_unix=1000, rng=random.Random(0))
    assert body == b"<rss>CACHED</rss>"
    matching = [r.getMessage() for r in caplog.records if "fetched" in r.getMessage()]
    assert len(matching) == 1
    msg = matching[0]
    assert "from cache" in msg
    assert "age 500s" in msg
    assert "304 not modified" in msg
    assert "downloaded" not in msg


# ---------------------------------------------------------------------------
# Per-row freshness short-circuit — when `cache_valid_until_unix` is in the
# future, fetch_feed_body returns the cached body without any network call.


class _ForbidOpener:
    """An opener that fails the test if `.open()` is called."""

    def __init__(self) -> None:
        self.calls = 0

    def open(self, *args, **kwargs):  # noqa: ANN001 — urllib OpenerDirector shape
        self.calls += 1
        raise AssertionError("opener.open() should not have been called")


def test_short_circuit_uses_per_row_expiry(conn):
    """`now_unix < cache_valid_until_unix` → cache hit, no network."""
    url = "https://example.invalid/feed.xml"
    save_cache(
        conn,
        source_url=url,
        body=b"<rss>FRESH</rss>",
        etag='"v1"',
        last_modified=None,
        fetched_at_unix=900,
        cache_valid_until_unix=2000,  # well into the future at now=1000
    )
    opener = _ForbidOpener()
    body = fetch_feed_body(url, conn, now_unix=1000, opener=opener, rng=random.Random(0))
    assert body == b"<rss>FRESH</rss>"
    assert opener.calls == 0
    # Short-circuit doesn't touch the row.
    cached = get_cached(conn, url)
    assert cached.fetched_at_unix == 900
    assert cached.cache_valid_until_unix == 2000


def test_expired_cache_falls_through_to_revalidation(conn):
    """`now_unix >= cache_valid_until_unix` → conditional GET fires."""
    handler = _make_handler(body=b"<rss/>", etag='"v1"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        save_cache(
            conn,
            source_url=url,
            body=b"<rss>OLD</rss>",
            etag='"v1"',
            last_modified=None,
            fetched_at_unix=1000,
            cache_valid_until_unix=1500,  # expires before now=1000+2*3600
        )
        body = fetch_feed_body(
            url, conn, now_unix=1000 + 2 * 3600, rng=random.Random(0),
        )
    # Server returned 304 (etag matched), fetcher returned cached body.
    assert body == b"<rss>OLD</rss>"
    # Server WAS hit (request was sent and 304'd), fetched_at bumped.
    assert handler.received_headers  # type: ignore[attr-defined]
    cached = get_cached(conn, url)
    assert cached.fetched_at_unix == 1000 + 2 * 3600
    # 304 path also extends the validity window.
    assert cached.cache_valid_until_unix > 1000 + 2 * 3600


def test_short_circuit_log_line_says_skipped_network(conn, caplog):
    """The short-circuit logs at DEBUG (not INFO) — only real network events
    (200 download, 304 revalidation) are operator-visible at INFO."""
    url = "https://example.invalid/feed.xml"
    save_cache(
        conn,
        source_url=url,
        body=b"<rss>FRESH</rss>",
        etag=None,
        last_modified=None,
        fetched_at_unix=900,
        cache_valid_until_unix=2500,  # ~25 min into the future at now=1000
    )
    with caplog.at_level("DEBUG", logger="news_agent.rss_fetcher"):
        fetch_feed_body(url, conn, now_unix=1000, opener=_ForbidOpener(), rng=random.Random(0))
    matching = [r for r in caplog.records if "fetched" in r.getMessage()]
    assert len(matching) == 1
    record = matching[0]
    msg = record.getMessage()
    assert record.levelname == "DEBUG"
    assert "from cache" in msg
    assert "age 100s" in msg
    assert "valid for another" in msg
    assert "skipped network" in msg
    assert "304" not in msg
    assert "downloaded" not in msg


def test_short_circuit_silent_at_default_info_level(conn, caplog):
    """At the daemon's default INFO level, the short-circuit emits nothing —
    that's the whole point of demoting it to DEBUG."""
    url = "https://example.invalid/feed.xml"
    save_cache(
        conn,
        source_url=url,
        body=b"<rss/>",
        etag=None,
        last_modified=None,
        fetched_at_unix=900,
        cache_valid_until_unix=2500,
    )
    with caplog.at_level("INFO", logger="news_agent.rss_fetcher"):
        fetch_feed_body(url, conn, now_unix=1000, opener=_ForbidOpener(), rng=random.Random(0))
    fetched_records = [r for r in caplog.records if "fetched" in r.getMessage()]
    assert fetched_records == []


# ---------------------------------------------------------------------------
# Per-row randomized expiry on writes — proves the jitter actually sprawls
# revalidation times across the 20–40 minute window.


def test_200_response_writes_cache_valid_until_within_jitter_range(conn):
    handler = _make_handler(body=b"<rss>X</rss>", etag='"v1"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        fetch_feed_body(url, conn, now_unix=1000, rng=random.Random(0))
    cached = get_cached(conn, url)
    assert (
        1000 + CACHE_VALIDITY_MIN_SECONDS
        <= cached.cache_valid_until_unix
        <= 1000 + CACHE_VALIDITY_MAX_SECONDS
    )


def test_304_response_extends_cache_valid_until(conn):
    """A 304 response refreshes both fetched_at and cache_valid_until_unix.

    Without this, a feed that's served with consistent ETags would never
    refresh its expiry window and would fall through to the network on
    every subsequent call after first expiry.
    """
    handler = _make_handler(body=b"<rss/>", etag='"v1"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        save_cache(
            conn,
            source_url=url,
            body=b"<rss>OLD</rss>",
            etag='"v1"',
            last_modified=None,
            fetched_at_unix=500,
            cache_valid_until_unix=0,  # expired
        )
        fetch_feed_body(url, conn, now_unix=1000, rng=random.Random(0))
    cached = get_cached(conn, url)
    assert cached.fetched_at_unix == 1000
    assert (
        1000 + CACHE_VALIDITY_MIN_SECONDS
        <= cached.cache_valid_until_unix
        <= 1000 + CACHE_VALIDITY_MAX_SECONDS
    )


def test_independent_feeds_get_independent_expiries(conn):
    """Sequential fetches with the same RNG yield different expiries — the
    whole point: feeds shouldn't all expire together."""
    handler = _make_handler(body=b"<rss/>", etag='"v1"')
    rng = random.Random(0)
    expiries: list[int] = []
    with _running_server(handler) as base:
        urls = [f"{base}/feed{i}.xml" for i in range(5)]
        for url in urls:
            fetch_feed_body(url, conn, now_unix=1000, rng=rng)
        for url in urls:
            cached = get_cached(conn, url)
            expiries.append(cached.cache_valid_until_unix)
    # All five values should have been written, and most should differ.
    # (With 5 random integers in a 1200-wide range, collisions are rare.)
    assert len(set(expiries)) >= 4, f"jitter not spreading: {expiries}"
    # All within the jitter range.
    for v in expiries:
        assert (
            1000 + CACHE_VALIDITY_MIN_SECONDS
            <= v
            <= 1000 + CACHE_VALIDITY_MAX_SECONDS
        )
