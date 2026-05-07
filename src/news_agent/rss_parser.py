"""Parse an RSS / Atom / RDF feed body into a list of :class:`Article`.

Wraps :mod:`feedparser` and normalises the heterogeneous output into a stable
shape the rest of the daemon can rely on:

- ``canonical_url`` runs every entry's link through ``url_canonicalize.canonicalize``
  so cross-feed dedupe sees the same string regardless of tracking params.
- ``published_at_unix`` is an integer Unix timestamp; entries with no date
  get a ``None`` (callers can treat that as "unknown publication time").
- Entries lacking a usable link are silently skipped — no link, no post.

feedparser is permissive; even malformed XML usually yields *some* entries.
We don't propagate parse warnings; if you need them, look at
``feedparser.parse(body).bozo`` directly.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from typing import Any

import feedparser

from news_agent.url_canonicalize import canonicalize

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Article:
    """One feed entry, normalised for the rest of the daemon."""

    title: str
    canonical_url: str       # post-canonicalisation, dedupe key
    raw_url: str             # original link, kept for the post body
    item_guid: str | None
    summary: str
    published_at_unix: int | None
    source_url: str          # the RSS feed URL this came from


def parse_feed(body: bytes, source_url: str) -> list[Article]:
    """Return all valid entries from a feed body."""
    parsed = feedparser.parse(body)
    if parsed.bozo:
        # Soft warning — feedparser is permissive and often returns useful
        # data despite bozo=True. Hard failures show up as an empty entries list.
        logger.debug(
            "feedparser flagged %s as bozo: %s", source_url, parsed.get("bozo_exception")
        )

    articles: list[Article] = []
    for entry in parsed.entries:
        article = _entry_to_article(entry, source_url)
        if article is not None:
            articles.append(article)
    return articles


def _entry_to_article(entry: Any, source_url: str) -> Article | None:
    raw_url = getattr(entry, "link", "") or ""
    raw_url = raw_url.strip()
    if not raw_url:
        return None

    title = (getattr(entry, "title", "") or "").strip()
    if not title:
        title = raw_url  # last-ditch fallback — better than empty

    summary = (getattr(entry, "summary", "") or "").strip()
    item_guid = getattr(entry, "id", None) or getattr(entry, "guid", None)
    if isinstance(item_guid, str):
        item_guid = item_guid.strip() or None
    elif item_guid is not None:
        item_guid = str(item_guid)

    published_at_unix = _coerce_published_time(entry)

    return Article(
        title=title,
        canonical_url=canonicalize(raw_url),
        raw_url=raw_url,
        item_guid=item_guid,
        summary=summary,
        published_at_unix=published_at_unix,
        source_url=source_url,
    )


def _coerce_published_time(entry: Any) -> int | None:
    """Return a Unix timestamp from any of feedparser's date fields, or None."""
    for attr in ("published_parsed", "updated_parsed", "created_parsed"):
        struct_time = getattr(entry, attr, None)
        if struct_time is not None:
            try:
                return calendar.timegm(struct_time)
            except (TypeError, ValueError, OverflowError):
                continue
    return None
