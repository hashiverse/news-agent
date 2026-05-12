"""Post one chosen article to hashiverse, or log what would have happened (dry-run).

The post body is hashiverse-flavoured HTML composed by calling Rust-side
fragment builders (single source of truth for the canonical schema, in
``hashiverse-lib/src/tools/plain_text_post.rs``):

  <div class="plugin-urlpreview-card">…</div>          # always present
  <p/>                                                  # only if hashtags non-empty
  <hashtag …>…</hashtag> <hashtag …>…</hashtag>         # one per identity hashtag

No separate article-title prefix — the title lives inside the preview card
as the link text.

Before posting we fetch the URL's OpenGraph metadata locally via
``news_agent.url_preview.fetch_url_preview(url)`` (stdlib urllib + html.parser,
HTTPS-only, 512 KB cap) — no hashiverse-server round-trip is involved. If
the fetch fails the post still goes out — the card falls back to
``article.title`` with no image/description. We never block the post on a
flaky preview fetch.

In both real and dry-run paths a row is written to the ``posts`` history
table with ``is_dry_run=0`` or ``=1``. The dedupe set used by the article
picker therefore reflects dry-run posts too — the scheduler doesn't pick
the same article twice within 24h regardless of mode.

**Dry-run does the same fetch + HTML construction as a real post**, then
logs the body instead of submitting. The whole point of dry-run is that
the operator sees exactly what would have hit the network — skipping the
preview fetch would defeat the preview.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from hashiverse_client import (
    convert_text_to_hashiverse_html_x_hashtag,
    convert_text_to_hashiverse_html_x_url_preview,
)

from news_agent.config import IdentityConfig
from news_agent.posts_db import record_post
from news_agent.rss_parser import Article
from news_agent.text_utils import strip_html
from news_agent.url_preview import UrlPreviewData, fetch_url_preview

logger = logging.getLogger(__name__)

# Re-export so existing `from news_agent.posting import UrlPreviewData` callers keep working.
__all__ = [
    "UrlPreviewData",
    "format_post_html",
    "post_or_dry_run",
    "resolve_post_fields",
]


def resolve_post_fields(article: Article, preview: UrlPreviewData) -> dict[str, str]:
    """Resolve the four card fields from a preview + article, applying fallbacks.

    Single source of truth for the fallback chain — `format_post_html` uses
    this to render, and `post_or_dry_run` uses it to log what we're about to
    submit. Keep these two readers in sync by going through one helper.
    """
    # Prefer the preview's resolved URL (it may have followed redirects);
    # fall back to the article's raw URL if preview didn't return one.
    url = preview.url or article.raw_url
    # Always show *something* clickable: prefer the OG title, fall back to
    # the article title, and to the URL itself if both are somehow blank.
    title = preview.title or article.title or url
    # Description: prefer the OG/twitter/meta description from the page, but
    # fall back to the RSS feed's own <description> (article.summary) — many
    # news sites omit OG tags, and the feed almost always carries a summary.
    # The summary may contain HTML markup; strip it so the description div
    # renders as clean text rather than literal `<p>` tags.
    description = preview.description or strip_html(article.summary)
    return {
        "url": url,
        "title": title,
        "description": description,
        "image_url": preview.image_url,
    }


def format_post_html(
    article: Article,
    preview: UrlPreviewData | None = None,
    hashtags: tuple[str, ...] = (),
) -> str:
    """Build the hashiverse HTML body for an article + optional URL preview.

    Card and hashtag fragments come from Rust (single source of truth for the
    canonical hashiverse HTML schema). The Python side just resolves the
    fallback chain (preview.title → article.title → url, etc.) and
    concatenates the fragments.
    """
    if preview is None:
        preview = UrlPreviewData()

    fields = resolve_post_fields(article, preview)
    body = convert_text_to_hashiverse_html_x_url_preview(
        title=fields["title"],
        description=fields["description"],
        image_url=fields["image_url"],
        url=fields["url"],
    )
    if hashtags:
        tags_html = " ".join(
            convert_text_to_hashiverse_html_x_hashtag(tag) for tag in hashtags
        )
        body = f"{body}<p/>{tags_html}"
    return body




def _fetch_preview_safely(url: str, log_label: str) -> UrlPreviewData:
    """Fetch OG data for ``url``. On failure, log a warning and return blanks."""
    try:
        return fetch_url_preview(url)
    except Exception as exc:  # noqa: BLE001 — preview failures must not block posting
        logger.warning(
            "%s: fetch_url_preview failed for %s: %s — posting without preview attrs",
            log_label,
            url,
            exc,
        )
        return UrlPreviewData()


def post_or_dry_run(
    *,
    client: Any,
    article: Article,
    identity: IdentityConfig,
    conn: sqlite3.Connection,
    dry_run: bool,
    now_unix: int | None = None,
) -> None:
    """Post the article (or log a dry-run line) and record the history row.

    Both branches do exactly the same fetch + HTML construction so a dry-run
    is a faithful preview of what production would have submitted. The only
    difference is whether we hand the body to ``client.submit_post`` or just
    log it.
    """
    if now_unix is None:
        now_unix = int(time.time())

    preview = _fetch_preview_safely(article.raw_url, identity.log_label)
    fields = resolve_post_fields(article, preview)
    logger.info(
        "%s post fields: url=%r title=%r description=%r image_url=%r",
        identity.log_label,
        fields["url"],
        fields["title"],
        fields["description"],
        fields["image_url"],
    )

    # Validity rule: the link is only worth posting if the preview itself
    # supplied a title, description, AND image. Article-side fallbacks (RSS
    # title/summary) deliberately do NOT rescue the rule — a link the page
    # can't preview properly (typical of bot-detection / stripped pages) is
    # treated as non-valid and skipped. The picker still dedupes against it
    # via posts_db so the daemon won't re-fetch the same dud every cycle.
    missing_preview_fields = [
        name for name in ("title", "description", "image_url")
        if not getattr(preview, name)
    ]
    if missing_preview_fields:
        logger.warning(
            "%s skipping post (preview missing %s): %s",
            identity.log_label,
            ", ".join(missing_preview_fields),
            article.raw_url,
        )
        record_post(
            conn,
            posted_at_unix=now_unix,
            identity_salt=identity.salt,
            canonical_url=article.canonical_url,
            source_url=article.source_url,
            title=article.title,
            item_guid=article.item_guid,
            is_dry_run=dry_run,
            is_skipped=True,
        )
        return

    html_body = format_post_html(article, preview, identity.hashtags)

    if dry_run:
        logger.info(
            "[DRY-RUN] %s would post: %r → %s",
            identity.log_label,
            article.title,
            article.raw_url,
        )
        logger.info("[DRY-RUN] body: %s", html_body)
    else:
        logger.info(
            "%s posting: %r → %s",
            identity.log_label,
            article.title,
            article.raw_url,
        )
        client.submit_post(html_body)

    record_post(
        conn,
        posted_at_unix=now_unix,
        identity_salt=identity.salt,
        canonical_url=article.canonical_url,
        source_url=article.source_url,
        title=article.title,
        item_guid=article.item_guid,
        is_dry_run=dry_run,
    )
