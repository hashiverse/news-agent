"""Post one chosen article to hashiverse, or log what would have happened (dry-run).

The post body is intentionally simple for now: title + raw URL on its own
line. The hashiverse client's ``post_with_preprocessing`` converts that to
HTML and handles hashtag detection. Richer formatting (per-source attribution,
hashtags from feed metadata, image attachments) is deferred.

In both real and dry-run paths a row is written to the ``posts`` history
table with ``is_dry_run=0`` or ``=1``. The dedupe set used by the article
picker therefore reflects dry-run posts too — the scheduler doesn't pick
the same article twice within 24h regardless of mode.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from news_agent.config import IdentityConfig
from news_agent.posts_db import record_post
from news_agent.rss_parser import Article

logger = logging.getLogger(__name__)


def format_post_text(article: Article) -> str:
    """Plaintext body fed to ``client.post_with_preprocessing``."""
    return f"{article.title}\n\n{article.raw_url}"


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

    On real posts, ``client.post_with_preprocessing`` is invoked — that
    submits to hashiverse and the call blocks until the post is committed
    (the hashiverse client handles PoW etc. internally).

    The hashiverse Python client API doesn't currently return the new post's
    ID synchronously from ``post_with_preprocessing`` — we record ``None``
    in the history. (When the API exposes the new post's ID we'll capture it.)
    """
    if now_unix is None:
        now_unix = int(time.time())

    text = format_post_text(article)

    if dry_run:
        logger.info(
            "[DRY-RUN] %s would post: %r → %s",
            identity.log_label,
            article.title,
            article.raw_url,
        )
        hashiverse_post_id: str | None = None
    else:
        logger.info(
            "%s posting: %r → %s",
            identity.log_label,
            article.title,
            article.raw_url,
        )
        client.post_with_preprocessing(text)
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
