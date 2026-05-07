"""Tests for posting.post_or_dry_run."""

from __future__ import annotations

import logging

import pytest

from news_agent import posting
from news_agent.config import IdentityConfig
from news_agent.posting import (
    UrlPreviewData,
    format_post_html,
    post_or_dry_run,
)
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
    """Records post calls. Preview fetching now happens locally — see news_agent.url_preview.

    Tests that need a controlled preview monkeypatch ``posting.fetch_url_preview``.
    """

    def __init__(self) -> None:
        self.posts: list[str] = []

    def post_without_preprocessing(self, html_body: str) -> None:
        self.posts.append(html_body)


def _patch_preview(monkeypatch, *, returns: UrlPreviewData | None = None, raises: Exception | None = None) -> list[str]:
    """Stub posting.fetch_url_preview. Returns a list that captures URLs it was called with."""
    captured_urls: list[str] = []

    def stub(url: str) -> UrlPreviewData:
        captured_urls.append(url)
        if raises is not None:
            raise raises
        return returns if returns is not None else UrlPreviewData()

    monkeypatch.setattr(posting, "fetch_url_preview", stub)
    return captured_urls


# ---------------------------------------------------------------------------
# format_post_html


def test_format_post_html_with_full_preview_renders_image_card():
    preview = UrlPreviewData(
        url="https://example.com/canonical",
        title="OG Title",
        description="OG description text",
        image_url="https://img.example/og.png",
    )
    html_body = format_post_html(_article(), preview)
    # Title from the article comes first, then the card.
    assert html_body.startswith("An article<br><br><div class=\"plugin-urlpreview-card\">")
    # With image: image-container wraps the img and the domain label.
    assert '<div class="plugin-urlpreview-card-image-container">' in html_body
    assert (
        '<img src="https://img.example/og.png" alt="" '
        'class="plugin-urlpreview-card-image unblur-image">'
    ) in html_body
    assert (
        '<div class="plugin-urlpreview-card-domain">example.com</div>'
    ) in html_body
    # Title link inside inner column. href uses preview.url, not the article's raw URL.
    assert (
        '<a class="plugin-urlpreview-card-title" href="https://example.com/canonical" '
        'rel="noopener noreferrer nofollow">OG Title</a>'
    ) in html_body
    assert (
        '<div class="plugin-urlpreview-card-description">OG description text</div>'
    ) in html_body


def test_format_post_html_without_image_moves_domain_into_inner_column():
    preview = UrlPreviewData(title="OG Title", description="desc")  # no image_url
    html_body = format_post_html(_article(), preview)
    # No image-container at all when image_url is blank.
    assert "plugin-urlpreview-card-image-container" not in html_body
    assert "<img " not in html_body
    # Domain div sits inside the inner column, above the title link.
    inner_open = html_body.index('<div class="plugin-urlpreview-card-inner">')
    domain_pos = html_body.index('<div class="plugin-urlpreview-card-domain">example.com</div>')
    title_pos = html_body.index('<a class="plugin-urlpreview-card-title"')
    assert inner_open < domain_pos < title_pos


def test_format_post_html_omits_description_div_when_description_blank():
    preview = UrlPreviewData(title="OG Title")  # no description
    html_body = format_post_html(_article(), preview)
    assert "plugin-urlpreview-card-description" not in html_body


def test_format_post_html_falls_back_to_article_title_when_preview_title_blank():
    preview = UrlPreviewData(image_url="https://img.example/og.png")  # no title
    html_body = format_post_html(_article(), preview)
    assert (
        '<a class="plugin-urlpreview-card-title" '
        'href="https://example.com/article?utm_source=x" '
        'rel="noopener noreferrer nofollow">An article</a>'
    ) in html_body


def test_format_post_html_falls_back_to_raw_url_when_preview_url_blank():
    preview = UrlPreviewData(title="OG", description="", image_url="")  # url left blank
    html_body = format_post_html(_article(), preview)
    assert 'href="https://example.com/article?utm_source=x"' in html_body


def test_format_post_html_html_escapes_special_chars():
    article = Article(
        title='Quote "marker" & <html>',
        canonical_url="https://example.com/article",
        raw_url="https://example.com/article",
        item_guid="urn:1",
        summary="x",
        published_at_unix=1_000_000,
        source_url="https://feed.example/rss",
    )
    preview = UrlPreviewData(
        title='He said "hi" & ran',
        description="<script>alert(1)</script>",
        image_url="https://img.example/o?a=1&b=2",
    )
    html_body = format_post_html(article, preview)
    # Article-title body text: quote=False → " left as-is, & < > escaped.
    assert "Quote \"marker\" &amp; &lt;html&gt;" in html_body
    # Title link text: quote=False so quotes pass through; & < > escaped.
    assert ">He said \"hi\" &amp; ran</a>" in html_body
    # Description text content: < > escaped to prevent injection.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_body
    # Image URL in attribute: quote=True so & is escaped to &amp;.
    assert 'src="https://img.example/o?a=1&amp;b=2"' in html_body


def test_format_post_html_converts_newlines_in_title_to_br():
    article = Article(
        title="Line one\nLine two",
        canonical_url="https://example.com/article",
        raw_url="https://example.com/article",
        item_guid="urn:1",
        summary="x",
        published_at_unix=1_000_000,
        source_url="https://feed.example/rss",
    )
    html_body = format_post_html(article)
    assert "Line one<br>Line two" in html_body


# ---------------------------------------------------------------------------
# post_or_dry_run


def test_dry_run_does_not_call_client(conn, caplog, monkeypatch):
    client = _FakeClient()
    captured = _patch_preview(monkeypatch)
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
    # Dry-run skips the preview fetch — no point burning a network round-trip.
    assert captured == []
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


def test_real_run_calls_client_and_records_history(conn, monkeypatch):
    client = _FakeClient()
    captured = _patch_preview(
        monkeypatch,
        returns=UrlPreviewData(
            url="https://example.com/article",
            title="OG Title",
            description="OG desc",
            image_url="https://img.example/og.png",
        ),
    )
    post_or_dry_run(
        client=client,
        article=_article(),
        identity=_identity(),
        conn=conn,
        dry_run=False,
        now_unix=2_000_000,
    )
    assert captured == ["https://example.com/article?utm_source=x"]
    assert len(client.posts) == 1
    body = client.posts[0]
    assert "An article" in body
    assert '<div class="plugin-urlpreview-card">' in body
    assert ">OG Title</a>" in body
    posts = posts_in_last_24h_for_identity(conn, SALT, 2_000_000)
    assert len(posts) == 1
    assert posts[0].is_dry_run is False


def test_real_run_posts_anyway_when_preview_fetch_fails(conn, caplog, monkeypatch):
    """Network/parsing failures during preview fetch must not block posting."""
    client = _FakeClient()
    _patch_preview(monkeypatch, raises=RuntimeError("preview fetch down"))
    with caplog.at_level(logging.WARNING):
        post_or_dry_run(
            client=client,
            article=_article(),
            identity=_identity(),
            conn=conn,
            dry_run=False,
            now_unix=2_000_000,
        )
    assert len(client.posts) == 1
    body = client.posts[0]
    # Card still renders, with article.title as the fallback link text and the
    # raw URL as href. No image-container, no description.
    assert '<div class="plugin-urlpreview-card">' in body
    assert 'href="https://example.com/article?utm_source=x"' in body
    assert ">An article</a>" in body
    assert "plugin-urlpreview-card-image-container" not in body
    assert "plugin-urlpreview-card-description" not in body
    assert any(
        "fetch_url_preview failed" in record.message for record in caplog.records
    )


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
