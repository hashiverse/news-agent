"""Tests for feed_cache_db — body + ETag/Last-Modified persistence."""

from __future__ import annotations

import pytest

from news_agent.feed_cache_db import (
    get_cached,
    save_cache,
    update_fetched_at,
)
from news_agent.state_db import open_state_db


@pytest.fixture
def conn(tmp_path):
    daemon_dir = tmp_path / "d"
    daemon_dir.mkdir()
    c = open_state_db(daemon_dir)
    yield c
    c.close()


URL = "https://example.com/feed.xml"


def test_get_missing_returns_none(conn):
    assert get_cached(conn, URL) is None


def test_save_then_get_round_trips(conn):
    save_cache(
        conn,
        source_url=URL,
        body=b"<rss/>",
        etag='"abc"',
        last_modified="Wed, 07 May 2026 09:00:00 GMT",
        fetched_at_unix=1234567890,
        cache_valid_until_unix=1234569690,
    )
    cached = get_cached(conn, URL)
    assert cached is not None
    assert cached.source_url == URL
    assert cached.body == b"<rss/>"
    assert cached.etag == '"abc"'
    assert cached.last_modified == "Wed, 07 May 2026 09:00:00 GMT"
    assert cached.fetched_at_unix == 1234567890
    assert cached.cache_valid_until_unix == 1234569690


def test_save_replaces_existing(conn):
    save_cache(
        conn,
        source_url=URL,
        body=b"v1",
        etag='"v1"',
        last_modified=None,
        fetched_at_unix=1,
        cache_valid_until_unix=1801,
    )
    save_cache(
        conn,
        source_url=URL,
        body=b"v2",
        etag='"v2"',
        last_modified=None,
        fetched_at_unix=2,
        cache_valid_until_unix=1802,
    )
    cached = get_cached(conn, URL)
    assert cached.body == b"v2"
    assert cached.etag == '"v2"'
    assert cached.fetched_at_unix == 2
    assert cached.cache_valid_until_unix == 1802


def test_save_with_null_etag_and_last_modified(conn):
    """Some servers don't supply caching headers — store NULL gracefully."""
    save_cache(
        conn,
        source_url=URL,
        body=b"x",
        etag=None,
        last_modified=None,
        fetched_at_unix=1,
        cache_valid_until_unix=1801,
    )
    cached = get_cached(conn, URL)
    assert cached.etag is None
    assert cached.last_modified is None


def test_update_fetched_at_refreshes_validity_window(conn):
    """304 path: bump both fetched_at and cache_valid_until_unix; leave the
    body, etag, and last_modified alone."""
    save_cache(
        conn,
        source_url=URL,
        body=b"original",
        etag='"v1"',
        last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
        fetched_at_unix=100,
        cache_valid_until_unix=1900,
    )
    update_fetched_at(conn, URL, fetched_at_unix=200, cache_valid_until_unix=2200)
    cached = get_cached(conn, URL)
    assert cached.body == b"original"
    assert cached.etag == '"v1"'
    assert cached.last_modified == "Wed, 01 Jan 2025 00:00:00 GMT"
    assert cached.fetched_at_unix == 200
    assert cached.cache_valid_until_unix == 2200


def test_multiple_urls_stored_independently(conn):
    save_cache(
        conn, source_url="https://a/", body=b"A", etag=None,
        last_modified=None, fetched_at_unix=1, cache_valid_until_unix=1801,
    )
    save_cache(
        conn, source_url="https://b/", body=b"B", etag=None,
        last_modified=None, fetched_at_unix=2, cache_valid_until_unix=1802,
    )
    a = get_cached(conn, "https://a/")
    b = get_cached(conn, "https://b/")
    assert a.body == b"A"
    assert b.body == b"B"
    assert a.cache_valid_until_unix == 1801
    assert b.cache_valid_until_unix == 1802
