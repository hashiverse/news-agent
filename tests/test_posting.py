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

    def submit_post(self, html_body: str) -> None:
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
    # Body starts with the preview card directly — no article-title prefix.
    assert html_body.startswith('<div class="plugin-urlpreview-card">')
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
    """No OG description AND no RSS summary → no description div at all."""
    preview = UrlPreviewData(title="OG Title")  # no description
    bare_article = Article(
        title="An article",
        canonical_url="https://example.com/article",
        raw_url="https://example.com/article",
        item_guid="urn:1",
        summary="",  # explicitly empty
        published_at_unix=1_000_000,
        source_url="https://feed.example/rss",
    )
    html_body = format_post_html(bare_article, preview)
    assert "plugin-urlpreview-card-description" not in html_body


def test_format_post_html_falls_back_to_article_summary_when_preview_description_blank():
    """If the page has no OG description, use the RSS feed's <description>."""
    article = Article(
        title="An article",
        canonical_url="https://example.com/article",
        raw_url="https://example.com/article",
        item_guid="urn:1",
        summary="A concise feed summary that the RSS provider gave us.",
        published_at_unix=1_000_000,
        source_url="https://feed.example/rss",
    )
    preview = UrlPreviewData(title="OG Title")  # no description on the page
    html_body = format_post_html(article, preview)
    assert (
        '<div class="plugin-urlpreview-card-description">'
        'A concise feed summary that the RSS provider gave us.'
        '</div>'
    ) in html_body


def test_format_post_html_strips_html_from_article_summary_fallback():
    """RSS summaries often contain markup — strip it so the card renders cleanly."""
    article = Article(
        title="An article",
        canonical_url="https://example.com/article",
        raw_url="https://example.com/article",
        item_guid="urn:1",
        summary="<p>First paragraph.</p>\n<p>Second &amp; sentence.</p>",
        published_at_unix=1_000_000,
        source_url="https://feed.example/rss",
    )
    html_body = format_post_html(article, UrlPreviewData(title="t"))
    assert (
        '<div class="plugin-urlpreview-card-description">'
        'First paragraph. Second &amp; sentence.'
        '</div>'
    ) in html_body
    # The literal `<p>` markup must NOT appear (would render as text otherwise).
    assert ">&lt;p&gt;" not in html_body


def test_format_post_html_preview_description_takes_precedence_over_summary():
    article = Article(
        title="An article",
        canonical_url="https://example.com/article",
        raw_url="https://example.com/article",
        item_guid="urn:1",
        summary="RSS summary fallback",
        published_at_unix=1_000_000,
        source_url="https://feed.example/rss",
    )
    preview = UrlPreviewData(title="t", description="OG description wins")
    html_body = format_post_html(article, preview)
    assert ">OG description wins<" in html_body
    assert "RSS summary fallback" not in html_body


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
    # The Rust card builder escapes & in URL attributes, so the raw URL's `&`
    # (none here) and `?` pass through; non-empty domain comes from urlparse.
    assert 'href="https://example.com/article?utm_source=x"' in html_body


def test_format_post_html_html_escapes_special_chars():
    """Rust _x_url_preview always escapes &, <, >, " in both attributes and
    text content (using the file-private html_escape in plain_text_post.rs)."""
    preview = UrlPreviewData(
        title='He said "hi" & ran',
        description="<script>alert(1)</script>",
        image_url="https://img.example/o?a=1&b=2",
        url="https://example.com/?q=1&r=2",
    )
    html_body = format_post_html(_article(), preview)
    # Title link text: " is escaped to &quot; (Rust's html_escape is broader
    # than Python's html.escape(quote=False)).
    assert ">He said &quot;hi&quot; &amp; ran</a>" in html_body
    # Description text content: < > escaped to prevent injection.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_body
    # Image URL in src attribute: & escaped to &amp;.
    assert 'src="https://img.example/o?a=1&amp;b=2"' in html_body
    # Article URL in href attribute: same.
    assert 'href="https://example.com/?q=1&amp;r=2"' in html_body


def test_format_post_html_appends_hashtag_elements_when_provided():
    """Hashtags become <hashtag> elements separated by spaces, after a <p/>."""
    html_body = format_post_html(_article(), UrlPreviewData(), hashtags=("news", "world"))
    assert "<p/>" in html_body
    # Each hashtag becomes a full <hashtag> element (no plain "#tag" text).
    assert '<hashtag hashtag="news">' in html_body
    assert '<hashtag hashtag="world">' in html_body
    # The body ends with the second hashtag's closing tag, with a single space
    # between the two hashtag elements.
    assert html_body.endswith("</hashtag>")
    assert "</hashtag> <hashtag" in html_body


def test_format_post_html_omits_hashtag_section_when_empty():
    html_body = format_post_html(_article(), UrlPreviewData())
    # Body ends with the closing </div> of the preview card — no trailing
    # <p/> or <hashtag> elements.
    assert html_body.endswith("</div>")
    assert "<p/>" not in html_body
    assert "<hashtag" not in html_body


def test_format_post_html_hashtag_attribute_lowercased_span_preserves_case():
    """Sanity-check that the Rust _x_hashtag wiring is in place."""
    html_body = format_post_html(_article(), UrlPreviewData(), hashtags=("MixedCase",))
    assert 'hashtag="mixedcase"' in html_body
    assert '<span class="plugin-hashtag-right">MixedCase</span>' in html_body


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
    # Dry-run does the same fetch + HTML construction as a real post — the
    # whole point is that the operator sees what would have hit the network.
    assert captured == ["https://example.com/article?utm_source=x"]
    assert any("[DRY-RUN]" in record.message for record in caplog.records)


def test_dry_run_logs_full_html_body(conn, caplog, monkeypatch):
    """Dry-run logs the would-be-posted HTML so operators can preview the post."""
    _patch_preview(
        monkeypatch,
        returns=UrlPreviewData(
            url="https://example.com/article",
            title="OG Title",
            description="OG desc",
            image_url="https://img.example/og.png",
        ),
    )
    with caplog.at_level(logging.INFO):
        post_or_dry_run(
            client=_FakeClient(),
            article=_article(),
            identity=_identity(),
            conn=conn,
            dry_run=True,
            now_unix=2_000_000,
        )
    body_messages = [
        record.message for record in caplog.records if "[DRY-RUN] body:" in record.message
    ]
    assert len(body_messages) == 1
    body = body_messages[0]
    # Body log carries the actual rendered card HTML, not just a summary.
    assert '<div class="plugin-urlpreview-card">' in body
    assert ">OG Title</a>" in body
    assert "https://img.example/og.png" in body


def test_post_logs_resolved_fields_before_posting(conn, caplog, monkeypatch):
    """Operator must be able to see the exact url/title/description/image_url
    that hit the network — protects against silent garbage like the YouTube
    `undefined` regression."""
    _patch_preview(
        monkeypatch,
        returns=UrlPreviewData(
            url="https://example.com/canonical",
            title="OG Title",
            description="OG desc",
            image_url="https://img.example/og.png",
        ),
    )
    with caplog.at_level(logging.INFO):
        post_or_dry_run(
            client=_FakeClient(),
            article=_article(),
            identity=_identity(),
            conn=conn,
            dry_run=True,
            now_unix=2_000_000,
        )
    field_lines = [r.message for r in caplog.records if "post fields:" in r.message]
    assert len(field_lines) == 1
    line = field_lines[0]
    assert "url='https://example.com/canonical'" in line
    assert "title='OG Title'" in line
    assert "description='OG desc'" in line
    assert "image_url='https://img.example/og.png'" in line


def test_post_field_log_uses_fallbacks_when_preview_blank(conn, caplog, monkeypatch):
    """When preview is empty, the log shows the same fallbacks format_post_html
    will use to render — article.title, article.raw_url, stripped summary."""
    _patch_preview(monkeypatch)  # returns UrlPreviewData() (all blanks)
    with caplog.at_level(logging.INFO):
        post_or_dry_run(
            client=_FakeClient(),
            article=_article(),
            identity=_identity(),
            conn=conn,
            dry_run=True,
            now_unix=2_000_000,
        )
    field_lines = [r.message for r in caplog.records if "post fields:" in r.message]
    assert len(field_lines) == 1
    line = field_lines[0]
    assert "url='https://example.com/article?utm_source=x'" in line
    assert "title='An article'" in line
    assert "description='summary'" in line
    assert "image_url=''" in line


def test_dry_run_records_history_with_dry_run_flag(conn, monkeypatch):
    _patch_preview(monkeypatch)
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
    # Card carries the OG title as the link text; the article title only
    # appears as a fallback when the preview title is blank (not the case here).
    assert '<div class="plugin-urlpreview-card">' in body
    assert ">OG Title</a>" in body
    posts = posts_in_last_24h_for_identity(conn, SALT, 2_000_000)
    assert len(posts) == 1
    assert posts[0].is_dry_run is False


def test_real_run_appends_identity_hashtags_to_post_body(conn, monkeypatch):
    client = _FakeClient()
    _patch_preview(monkeypatch)
    identity = IdentityConfig(
        salt=SALT,
        nickname="Test",
        status="x",
        max_posts_per_day=5,
        sources=("https://feed.example/rss",),
        hashtags=("news", "world"),
    )
    post_or_dry_run(
        client=client,
        article=_article(),
        identity=identity,
        conn=conn,
        dry_run=False,
        now_unix=2_000_000,
    )
    assert len(client.posts) == 1
    body = client.posts[0]
    assert "<p/>" in body
    assert '<hashtag hashtag="news">' in body
    assert '<hashtag hashtag="world">' in body
    assert body.endswith("</hashtag>")


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
    # raw URL as href. No image-container (preview fetch failed → no image_url).
    # The description div picks up the article.summary fallback.
    assert '<div class="plugin-urlpreview-card">' in body
    assert 'href="https://example.com/article?utm_source=x"' in body
    assert ">An article</a>" in body
    assert "plugin-urlpreview-card-image-container" not in body
    assert any(
        "fetch_url_preview failed" in record.message for record in caplog.records
    )


def test_default_now_unix_is_close_to_real_clock(conn, monkeypatch):
    """When now_unix is omitted the function uses time.time() — verify roughly."""
    import time as time_module
    _patch_preview(monkeypatch)
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
