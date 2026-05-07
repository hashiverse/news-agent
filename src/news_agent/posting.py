"""Post one chosen article to hashiverse, or log what would have happened (dry-run).

The post body is hashiverse-flavoured HTML containing a fully-rendered URL
preview card — NOT the ``<urlpreview>`` editor element. Tiptap plugins only
run while *editing* a post; when other clients *view* a post they render
plain HTML, so the on-wire format is the structural HTML produced by the
web client's ``build_card_dom`` (`UrlPreviewExtension.ts`):

    <div class="plugin-urlpreview-card">
      <div class="plugin-urlpreview-card-image-container">
        <img src="…" alt="" class="plugin-urlpreview-card-image unblur-image">
        <div class="plugin-urlpreview-card-domain">domain.example</div>
      </div>
      <div class="plugin-urlpreview-card-inner">
        <a class="plugin-urlpreview-card-title" href="…" rel="noopener noreferrer nofollow">Title</a>
        <div class="plugin-urlpreview-card-description">Description</div>
      </div>
    </div>

The ``plugin-urlpreview-card*`` CSS classes are provided by the consuming
client at view time, so we just need to use them — no inline styles.

Before posting we fetch the URL's OpenGraph metadata locally via
``news_agent.url_preview.fetch_url_preview(url)`` (stdlib urllib + html.parser,
HTTPS-only, 512 KB cap) — no hashiverse-server round-trip is involved. If
the fetch fails the post still goes out — the card falls back to
``article.title`` with no image/description. We never block the post on a
flaky preview fetch.

In both real and dry-run paths a row is written to the ``posts`` history
table with ``is_dry_run=0`` or ``=1``. The dedupe set used by the article
picker therefore reflects dry-run posts too — the scheduler doesn't pick
the same article twice within 24h regardless of mode. Dry-run skips the
preview fetch (no point burning a server round-trip for a log line).
"""

from __future__ import annotations

import html
import logging
import sqlite3
import time
from typing import Any
from urllib.parse import urlparse

from news_agent.config import IdentityConfig
from news_agent.posts_db import record_post
from news_agent.rss_parser import Article
from news_agent.url_preview import UrlPreviewData, fetch_url_preview

logger = logging.getLogger(__name__)

# Re-export so existing `from news_agent.posting import UrlPreviewData` callers keep working.
__all__ = ["UrlPreviewData", "format_post_html", "post_or_dry_run"]


def format_post_html(article: Article, preview: UrlPreviewData | None = None) -> str:
    """Build the hashiverse HTML body for an article + optional URL preview.

    The body is the article's title (HTML-escaped, newlines → ``<br>``)
    followed by a rendered preview card (see module docstring for shape).
    """
    if preview is None:
        preview = UrlPreviewData()

    title_html = (
        html.escape(article.title, quote=False)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br>")
    )

    # Prefer the preview's resolved URL (it may have followed redirects);
    # fall back to the article's raw URL if preview didn't return one.
    url = preview.url or article.raw_url
    domain = urlparse(url).hostname or ""
    # Always show *something* clickable: prefer the OG title, fall back to
    # the article title, and to the URL itself if both are somehow blank.
    card_title = preview.title or article.title or url
    domain_text = domain or url

    card = _build_url_preview_card(
        url=url,
        domain=domain_text,
        title=card_title,
        description=preview.description,
        image_url=preview.image_url,
    )

    return f"{title_html}<br><br>{card}"


def _build_url_preview_card(
    *,
    url: str,
    domain: str,
    title: str,
    description: str,
    image_url: str,
) -> str:
    """Render the preview card HTML.

    Mirrors ``build_card_dom`` in ``hashiverse-client-web``'s UrlPreview
    extension: when an image is present, the domain label sits inside the
    image container; without an image, it moves into the inner column above
    the title.
    """
    parts: list[str] = ['<div class="plugin-urlpreview-card">']

    if image_url:
        parts.append('<div class="plugin-urlpreview-card-image-container">')
        parts.append(
            f'<img src="{html.escape(image_url, quote=True)}" alt="" '
            f'class="plugin-urlpreview-card-image unblur-image">'
        )
        parts.append(
            '<div class="plugin-urlpreview-card-domain">'
            f"{html.escape(domain, quote=False)}"
            "</div>"
        )
        parts.append("</div>")

    parts.append('<div class="plugin-urlpreview-card-inner">')
    if not image_url:
        parts.append(
            '<div class="plugin-urlpreview-card-domain">'
            f"{html.escape(domain, quote=False)}"
            "</div>"
        )
    parts.append(
        f'<a class="plugin-urlpreview-card-title" href="{html.escape(url, quote=True)}" '
        f'rel="noopener noreferrer nofollow">'
        f"{html.escape(title, quote=False)}"
        "</a>"
    )
    if description:
        parts.append(
            '<div class="plugin-urlpreview-card-description">'
            f"{html.escape(description, quote=False)}"
            "</div>"
        )
    parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


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

    On real posts: fetch URL preview, build hashiverse HTML, submit via
    ``client.post_without_preprocessing`` (the body is already HTML — using
    ``post_with_preprocessing`` would double-escape the ``<urlpreview>`` tag).

    The hashiverse Python client API doesn't currently return the new post's
    ID synchronously from ``post_without_preprocessing`` — we record ``None``
    in the history. (When the API exposes the new post's ID we'll capture it.)
    """
    if now_unix is None:
        now_unix = int(time.time())

    if dry_run:
        logger.info(
            "[DRY-RUN] %s would post: %r → %s",
            identity.log_label,
            article.title,
            article.raw_url,
        )
        hashiverse_post_id: str | None = None
    else:
        preview = _fetch_preview_safely(article.raw_url, identity.log_label)
        html_body = format_post_html(article, preview)
        logger.info(
            "%s posting: %r → %s",
            identity.log_label,
            article.title,
            article.raw_url,
        )
        client.post_without_preprocessing(html_body)
        hashiverse_post_id = None  # see docstring

    record_post(
        conn,
        posted_at_unix=now_unix,
        identity_salt=identity.salt,
        canonical_url=article.canonical_url,
        source_url=article.source_url,
        title=article.title,
        item_guid=article.item_guid,
        hashiverse_post_id=hashiverse_post_id,
        is_dry_run=dry_run,
    )
