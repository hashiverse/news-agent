"""Tests for posts_db — recording and querying the posts history."""

from __future__ import annotations

from pathlib import Path

import pytest

from news_agent.posts_db import (
    ONE_DAY_SECONDS,
    posted_canonical_urls_in_last_24h,
    posts_in_last_24h_for_identity,
    record_post,
)
from news_agent.state_db import open_state_db


@pytest.fixture
def conn(tmp_path):
    daemon_dir = tmp_path / "d"
    daemon_dir.mkdir()
    c = open_state_db(daemon_dir)
    yield c
    c.close()


def _post(conn, *, when: int, salt: str, url: str, dry_run: bool = False) -> None:
    record_post(
        conn,
        posted_at_unix=when,
        identity_salt=salt,
        canonical_url=url,
        source_url="https://feed.example/rss",
        title="t",
        item_guid="g",
        is_dry_run=dry_run,
    )


def test_recent_urls_are_returned(conn):
    now = 1_000_000
    _post(conn, when=now - 100, salt="A", url="https://x.com/a")
    _post(conn, when=now - 50, salt="B", url="https://x.com/b")
    urls = posted_canonical_urls_in_last_24h(conn, now)
    assert urls == {"https://x.com/a", "https://x.com/b"}


def test_old_urls_are_excluded(conn):
    now = 1_000_000
    _post(conn, when=now - ONE_DAY_SECONDS - 1, salt="A", url="https://x.com/old")
    _post(conn, when=now - 100, salt="A", url="https://x.com/new")
    assert posted_canonical_urls_in_last_24h(conn, now) == {"https://x.com/new"}


def test_dedupe_set_collapses_same_url_from_multiple_identities(conn):
    """Cross-identity dedupe: same canonical URL from A and B → one entry."""
    now = 1_000_000
    _post(conn, when=now - 100, salt="A", url="https://x.com/shared")
    _post(conn, when=now - 50, salt="B", url="https://x.com/shared")
    assert posted_canonical_urls_in_last_24h(conn, now) == {"https://x.com/shared"}


def test_dry_run_posts_count_for_dedupe(conn):
    now = 1_000_000
    _post(conn, when=now - 100, salt="A", url="https://x.com/dry", dry_run=True)
    # Dry-run posts are still in the dedupe set so the scheduler doesn't
    # re-pick the same article on the next cycle.
    assert posted_canonical_urls_in_last_24h(conn, now) == {"https://x.com/dry"}


def test_per_identity_posts_returns_only_that_identity_oldest_first(conn):
    now = 1_000_000
    _post(conn, when=now - 300, salt="A", url="https://x.com/a1")
    _post(conn, when=now - 200, salt="B", url="https://x.com/b1")
    _post(conn, when=now - 100, salt="A", url="https://x.com/a2")

    a_posts = posts_in_last_24h_for_identity(conn, "A", now)
    assert [p.canonical_url for p in a_posts] == ["https://x.com/a1", "https://x.com/a2"]
    assert all(p.identity_salt == "A" for p in a_posts)

    b_posts = posts_in_last_24h_for_identity(conn, "B", now)
    assert [p.canonical_url for p in b_posts] == ["https://x.com/b1"]


def test_per_identity_posts_excludes_old(conn):
    now = 1_000_000
    _post(conn, when=now - ONE_DAY_SECONDS - 100, salt="A", url="https://x.com/old")
    _post(conn, when=now - 50, salt="A", url="https://x.com/new")
    posts = posts_in_last_24h_for_identity(conn, "A", now)
    assert [p.canonical_url for p in posts] == ["https://x.com/new"]


def test_record_post_dry_run_round_trips_fields(conn):
    now = 1_000_000
    record_post(
        conn,
        posted_at_unix=now,
        identity_salt="salt-X",
        canonical_url="https://example.com/x",
        source_url="https://example.com/rss",
        title="hello",
        item_guid="urn:1",
        is_dry_run=True,
    )
    posts = posts_in_last_24h_for_identity(conn, "salt-X", now)
    assert len(posts) == 1
    p = posts[0]
    assert p.posted_at_unix == now
    assert p.identity_salt == "salt-X"
    assert p.canonical_url == "https://example.com/x"
    assert p.source_url == "https://example.com/rss"
    assert p.title == "hello"
    assert p.item_guid == "urn:1"
    assert p.is_dry_run is True


def test_record_post_real_run_round_trips_fields(conn):
    now = 1_000_000
    record_post(
        conn,
        posted_at_unix=now,
        identity_salt="salt-Y",
        canonical_url="https://example.com/y",
        source_url="https://example.com/rss",
        title="real",
        item_guid=None,
        is_dry_run=False,
    )
    posts = posts_in_last_24h_for_identity(conn, "salt-Y", now)
    assert posts[0].is_dry_run is False
    assert posts[0].item_guid is None


def test_empty_db_returns_empty(conn):
    now = 1_000_000
    assert posted_canonical_urls_in_last_24h(conn, now) == set()
    assert posts_in_last_24h_for_identity(conn, "any", now) == []


# ---------------------------------------------------------------------------
# is_skipped semantics
# ---------------------------------------------------------------------------


def test_record_post_with_is_skipped_persists_the_flag(conn):
    record_post(
        conn,
        posted_at_unix=1_000_000,
        identity_salt="A",
        canonical_url="https://x.com/skipped",
        source_url="https://feed.example/rss",
        title="t",
        item_guid="g",
        is_dry_run=False,
        is_skipped=True,
    )
    row = conn.execute(
        "SELECT is_skipped, is_dry_run FROM posts WHERE canonical_url=?",
        ("https://x.com/skipped",),
    ).fetchone()
    assert row == (1, 0)


def test_record_post_default_is_skipped_false(conn):
    """Backward-compat: callers that don't pass is_skipped get is_skipped=0."""
    record_post(
        conn,
        posted_at_unix=1_000_000,
        identity_salt="A",
        canonical_url="https://x.com/normal",
        source_url="https://feed.example/rss",
        title="t",
        item_guid="g",
        is_dry_run=False,
    )
    row = conn.execute(
        "SELECT is_skipped FROM posts WHERE canonical_url=?",
        ("https://x.com/normal",),
    ).fetchone()
    assert row == (0,)


def test_skipped_rows_participate_in_cross_identity_dedupe(conn):
    """The picker's 24h dedupe MUST see skipped URLs — otherwise a permafail
    YouTube URL would be re-fetched on every cycle."""
    now = 1_000_000
    record_post(
        conn,
        posted_at_unix=now - 100,
        identity_salt="A",
        canonical_url="https://x.com/skipped",
        source_url="https://feed.example/rss",
        title="t",
        item_guid="g",
        is_dry_run=False,
        is_skipped=True,
    )
    assert posted_canonical_urls_in_last_24h(conn, now) == {"https://x.com/skipped"}


def test_skipped_rows_excluded_from_per_identity_quota(conn):
    """The scheduler's per-identity quota query MUST exclude skipped rows.
    A skipped article didn't consume a post slot."""
    now = 1_000_000
    record_post(
        conn,
        posted_at_unix=now - 200,
        identity_salt="A",
        canonical_url="https://x.com/posted",
        source_url="https://feed.example/rss",
        title="real",
        item_guid="g1",
        is_dry_run=False,
        is_skipped=False,
    )
    record_post(
        conn,
        posted_at_unix=now - 100,
        identity_salt="A",
        canonical_url="https://x.com/skipped",
        source_url="https://feed.example/rss",
        title="dud",
        item_guid="g2",
        is_dry_run=False,
        is_skipped=True,
    )
    posts = posts_in_last_24h_for_identity(conn, "A", now)
    assert len(posts) == 1
    assert posts[0].canonical_url == "https://x.com/posted"
    assert posts[0].is_skipped is False


def test_post_record_carries_is_skipped(conn):
    """Round-trip: PostRecord exposes is_skipped to callers (default False for
    non-skipped rows)."""
    now = 1_000_000
    record_post(
        conn,
        posted_at_unix=now - 100,
        identity_salt="A",
        canonical_url="https://x.com/posted",
        source_url="https://feed.example/rss",
        title="t",
        item_guid="g",
        is_dry_run=False,
    )
    posts = posts_in_last_24h_for_identity(conn, "A", now)
    assert posts[0].is_skipped is False
