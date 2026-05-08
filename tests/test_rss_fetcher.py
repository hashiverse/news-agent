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
        fetch_feed_body(url, conn, now_unix=1000)
        body = fetch_feed_body(url, conn, now_unix=2000)

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
        fetch_feed_body(url, conn, now_unix=1000)
        fetch_feed_body(url, conn, now_unix=2000)

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
        body = fetch_feed_body(url, conn, now_unix=1000)

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
    body = fetch_feed_body(url, conn, timeout=1, now_unix=1000)
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
        body = fetch_feed_body(url, conn, now_unix=1000)

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
            body = fetch_feed_body(url, conn, now_unix=1000)
    assert body == b"<rss>CACHED</rss>"
    matching = [r.getMessage() for r in caplog.records if "fetched" in r.getMessage()]
    assert len(matching) == 1
    msg = matching[0]
    assert "from cache" in msg
    assert "age 500s" in msg
    assert "downloaded" not in msg
