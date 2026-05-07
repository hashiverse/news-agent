"""Tests for posting.post_or_dry_run."""

from __future__ import annotations

import logging

import pytest

from news_agent.config import IdentityConfig
from news_agent.posting import format_post_text, post_or_dry_run
from news_agent.posts_db import posts_in_last_24h_for_identity
from news_agent.rss_parser import Article
from news_agent.state_db import open_state_db


SALT = "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"


@pytest.fixture
def conn(tmp_path):
    daemon_dir = tmp_path / "d"
    daemon_dir.mkdir()
    c = open_state_db(daemon_dir)
    yield c
    c.close()


def _identity() -> IdentityConfig:
    return IdentityConfig(
        salt=SALT,
        nickname="Test",
        status="x",
        max_posts_per_day=5,
        sources=("https://feed.example/rss",),
    )


def _article() -> Article:
    return Article(
        title="An article",
        canonical_url="https://example.com/article",
        raw_url="https://example.com/article?utm_source=x",
        item_guid="urn:1",
        summary="summary",
        published_at_unix=1_000_000,
        source_url="https://feed.example/rss",
    )


class _FakeClient:
    """Records every post call. Real hashiverse-client is not invoked."""

    def __init__(self) -> None:
        self.posts: list[str] = []

    def post_with_preprocessing(self, text: str) -> None:
        self.posts.append(text)


def test_format_post_text_includes_title_and_raw_url():
    text = format_post_text(_article())
    assert "An article" in text
    assert "https://example.com/article?utm_source=x" in text


def test_dry_run_does_not_call_client(conn, caplog):
    client = _FakeClient()
    with caplog.at_level(logging.INFO):
        post_or_dry_run(
            client=client,
            article=_article(),
            identity=_identity(),
            conn=conn,
            dry_run=True,
            now_unix=2_000_000,
        )
    assert client.posts == []
    assert any("[DRY-RUN]" in record.message for record in caplog.records)


def test_dry_run_records_history_with_dry_run_flag(conn):
    post_or_dry_run(
        client=_FakeClient(),
        article=_article(),
        identity=_identity(),
        conn=conn,
        dry_run=True,
        now_unix=2_000_000,
    )
    posts = posts_in_last_24h_for_identity(conn, SALT, 2_000_000)
    assert len(posts) == 1
    assert posts[0].is_dry_run is True
    assert posts[0].hashiverse_post_id is None
    assert posts[0].canonical_url == "https://example.com/article"
    assert posts[0].source_url == "https://feed.example/rss"
    assert posts[0].title == "An article"
    assert posts[0].item_guid == "urn:1"


def test_real_run_calls_client_and_records_history(conn):
    client = _FakeClient()
    post_or_dry_run(
        client=client,
        article=_article(),
        identity=_identity(),
        conn=conn,
        dry_run=False,
        now_unix=2_000_000,
    )
    assert len(client.posts) == 1
    assert "An article" in client.posts[0]
    posts = posts_in_last_24h_for_identity(conn, SALT, 2_000_000)
    assert len(posts) == 1
    assert posts[0].is_dry_run is False


def test_default_now_unix_is_close_to_real_clock(conn):
    """When now_unix is omitted the function uses time.time() — verify roughly."""
    import time as time_module
    before = int(time_module.time())
    post_or_dry_run(
        client=_FakeClient(),
        article=_article(),
        identity=_identity(),
        conn=conn,
        dry_run=True,
    )
    after = int(time_module.time())
    posts = posts_in_last_24h_for_identity(conn, SALT, after + 10)
    assert before <= posts[0].posted_at_unix <= after
