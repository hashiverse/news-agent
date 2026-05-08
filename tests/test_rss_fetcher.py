"""Tests for rss_fetcher — conditional GET with SQLite-backed cache."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from news_agent.feed_cache_db import get_cached, save_cache
from news_agent.rss_fetcher import FeedFetchError, fetch_feed_body
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


def test_first_fetch_writes_cache(conn):
    handler = _make_handler(body=b"<rss>v1</rss>", etag='"abc"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        body = fetch_feed_body(url, conn, now_unix=1000)
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
        # cache_freshness_window=0 forces the conditional-GET path on every
        # call so we can exercise the 304 logic.
        fetch_feed_body(url, conn, now_unix=1000, cache_freshness_window=0)
        body = fetch_feed_body(url, conn, now_unix=2000, cache_freshness_window=0)

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
        fetch_feed_body(url, conn, now_unix=1000, cache_freshness_window=0)
        fetch_feed_body(url, conn, now_unix=2000, cache_freshness_window=0)

    ims = handler.received_headers[1].get("If-Modified-Since")  # type: ignore[attr-defined]
    assert ims == "Wed, 01 Jan 2025 00:00:00 GMT"


def test_500_with_existing_cache_returns_stale_body(conn):
    """Server starts returning 500 — fetcher returns the cached body."""
    handler = _make_handler(error_status=500)
    url = "http://example.invalid/feed.xml"
    save_cache(
        conn,
        source_url=url,
        body=b"<rss>cached</rss>",
        etag='"old"',
        last_modified=None,
        fetched_at_unix=500,
    )
    # Run via a real localhost server so urllib actually connects.
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        # Reseed the cache under the actual URL the server's listening at.
        save_cache(
            conn,
            source_url=url,
            body=b"<rss>cached</rss>",
            etag='"old"',
            last_modified=None,
            fetched_at_unix=500,
        )
        # Disable the freshness-window short-circuit so we actually exercise
        # the error-fallback branch, not the cache-reuse one.
        body = fetch_feed_body(url, conn, now_unix=1000, cache_freshness_window=0)

    assert body == b"<rss>cached</rss>"
    # Cache content untouched (no save_cache, no update_fetched_at on this path).
    cached = get_cached(conn, url)
    assert cached.fetched_at_unix == 500


def test_500_without_cache_raises(conn):
    handler = _make_handler(error_status=500)
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        with pytest.raises(FeedFetchError):
            fetch_feed_body(url, conn, now_unix=1000)


def test_unreachable_host_with_no_cache_raises(conn):
    # Port 1 is reserved and won't accept connections.
    with pytest.raises(FeedFetchError):
        fetch_feed_body("http://127.0.0.1:1/x", conn, timeout=1, now_unix=1000)


def test_unreachable_host_with_cache_returns_stale(conn):
    url = "http://127.0.0.1:1/x"
    save_cache(
        conn,
        source_url=url,
        body=b"stale",
        etag=None,
        last_modified=None,
        fetched_at_unix=100,
    )
    # Disable the freshness window so the network attempt actually fires
    # and we exercise the error-fallback branch.
    body = fetch_feed_body(
        url, conn, timeout=1, now_unix=1000, cache_freshness_window=0
    )
    assert body == b"stale"


def test_cache_replaces_on_200(conn):
    """200 response replaces the cached body even when one already exists."""
    handler = _make_handler(body=b"<rss>NEW</rss>", etag='"new"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        # Prime with a different body+ETag to force the conditional fetch.
        save_cache(
            conn,
            source_url=url,
            body=b"<rss>OLD</rss>",
            etag='"old"',
            last_modified=None,
            fetched_at_unix=500,
        )
        body = fetch_feed_body(url, conn, now_unix=1000, cache_freshness_window=0)

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
            fetch_feed_body(url, conn, now_unix=1000)
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
        )
        with caplog.at_level("INFO", logger="news_agent.rss_fetcher"):
            # Disable the freshness-window short-circuit so the test actually
            # exercises the 304 branch (otherwise the short-circuit fires first).
            body = fetch_feed_body(
                url, conn, now_unix=1000, cache_freshness_window=0
            )
    assert body == b"<rss>CACHED</rss>"
    matching = [r.getMessage() for r in caplog.records if "fetched" in r.getMessage()]
    assert len(matching) == 1
    msg = matching[0]
    assert "from cache" in msg
    assert "age 500s" in msg
    assert "304 not modified" in msg
    assert "downloaded" not in msg


# ---------------------------------------------------------------------------
# Freshness-window short-circuit — when the cache is younger than
# CACHE_FRESHNESS_WINDOW_SECONDS, fetch_feed_body returns it without
# making any network request at all.


class _ForbidOpener:
    """An opener that fails the test if `.open()` is called."""

    def __init__(self) -> None:
        self.calls = 0

    def open(self, *args, **kwargs):  # noqa: ANN001 — urllib OpenerDirector shape
        self.calls += 1
        raise AssertionError("opener.open() should not have been called")


def test_fresh_cache_short_circuits_network_entirely(conn):
    """Cache age < freshness window → no network request, return cached body."""
    url = "https://example.invalid/feed.xml"
    save_cache(
        conn,
        source_url=url,
        body=b"<rss>FRESH</rss>",
        etag='"v1"',
        last_modified=None,
        fetched_at_unix=900,
    )
    opener = _ForbidOpener()
    body = fetch_feed_body(url, conn, now_unix=1000, opener=opener)
    assert body == b"<rss>FRESH</rss>"
    assert opener.calls == 0
    # Cache untouched by the short-circuit (no fetched_at bump).
    cached = get_cached(conn, url)
    assert cached.fetched_at_unix == 900


def test_stale_cache_falls_through_to_revalidation(conn):
    """Cache age >= freshness window → conditional GET fires."""
    handler = _make_handler(body=b"<rss/>", etag='"v1"')
    with _running_server(handler) as base:
        url = f"{base}/feed.xml"
        # 2 hours old > the 30-minute default freshness window.
        save_cache(
            conn,
            source_url=url,
            body=b"<rss>OLD</rss>",
            etag='"v1"',
            last_modified=None,
            fetched_at_unix=1000,
        )
        body = fetch_feed_body(url, conn, now_unix=1000 + 2 * 3600)
    # Server returned 304 (etag matched), fetcher returned cached body.
    assert body == b"<rss>OLD</rss>"
    # Server WAS hit (request was sent and 304'd), fetched_at bumped.
    assert handler.received_headers  # type: ignore[attr-defined]
    cached = get_cached(conn, url)
    assert cached.fetched_at_unix == 1000 + 2 * 3600


def test_fresh_cache_log_line_says_skipped_network(conn, caplog):
    url = "https://example.invalid/feed.xml"
    save_cache(
        conn,
        source_url=url,
        body=b"<rss>FRESH</rss>",
        etag=None,
        last_modified=None,
        fetched_at_unix=900,
    )
    with caplog.at_level("INFO", logger="news_agent.rss_fetcher"):
        fetch_feed_body(url, conn, now_unix=1000, opener=_ForbidOpener())
    matching = [r.getMessage() for r in caplog.records if "fetched" in r.getMessage()]
    assert len(matching) == 1
    msg = matching[0]
    assert "from cache" in msg
    assert "age 100s" in msg
    assert "fresh" in msg
    assert "skipped network" in msg
    assert "304" not in msg
    assert "downloaded" not in msg


def test_freshness_window_zero_disables_short_circuit(conn):
    """cache_freshness_window=0 forces every call through the conditional-GET
    path. Used in the rest of this file to test the network branches."""
    url = "https://example.invalid/feed.xml"
    save_cache(
        conn,
        source_url=url,
        body=b"<rss>FRESH</rss>",
        etag=None,
        last_modified=None,
        fetched_at_unix=999,
    )
    opener = _ForbidOpener()
    # Without the kwarg this would short-circuit; with cache_freshness_window=0
    # it must call the opener (which raises).
    with pytest.raises(AssertionError, match="opener.open"):
        fetch_feed_body(
            url, conn, now_unix=1000, opener=opener, cache_freshness_window=0
        )
    assert opener.calls == 1
