"""OPML 2.0 loader → list[FeedSpec].

Reads an OPML file produced by an upstream curator (e.g. github.com/hashiverse/news-feeds)
and returns the structured list of feeds. The OPML ``category`` attribute is
parsed into both raw paths (used for selector matching) and leaf tags (used for
hashtag injection in later phases).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


@dataclass(frozen=True)
class FeedSpec:
    """One RSS source from the OPML file."""

    title: str
    feed_url: str
    site_url: str
    raw_categories: tuple[str, ...]  # full slash-prefixed paths, e.g. "/topic/science"
    tags: tuple[str, ...]            # leaf segments only, lowercased, e.g. "science"


class OpmlParseError(ValueError):
    """Raised when the OPML file is malformed or missing required elements."""


def _parse_categories(category_attr: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a comma-separated ``category`` attribute into raw paths and leaf tags.

    Empty entries are dropped. Duplicates are preserved in order of first
    appearance. Leaf segments are lowercased.
    """
    raw_paths: list[str] = []
    leaf_tags: list[str] = []
    seen_paths: set[str] = set()
    seen_tags: set[str] = set()

    for entry in category_attr.split(","):
        path = entry.strip()
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        raw_paths.append(path)

        # Leaf segment: last non-empty path component, lowercased.
        segments = [s for s in path.split("/") if s]
        if not segments:
            continue
        leaf = segments[-1].lower()
        if leaf in seen_tags:
            continue
        seen_tags.add(leaf)
        leaf_tags.append(leaf)

    return tuple(raw_paths), tuple(leaf_tags)


def load_opml(path: Path) -> list[FeedSpec]:
    """Parse an OPML 2.0 file and return all RSS outlines as FeedSpec rows."""
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError as exc:
        raise OpmlParseError(f"OPML at {path} is not well-formed XML: {exc}") from exc

    root = tree.getroot()
    if root.tag != "opml":
        raise OpmlParseError(
            f"OPML at {path} has root <{root.tag}>, expected <opml>"
        )

    feeds: list[FeedSpec] = []
    for outline in root.iter("outline"):
        if outline.attrib.get("type") != "rss":
            continue

        feed_url = outline.attrib.get("xmlUrl", "").strip()
        if not feed_url:
            # Outlines without xmlUrl can't be mirrored — skip silently.
            continue

        title = outline.attrib.get("text", "").strip() or feed_url
        site_url = outline.attrib.get("htmlUrl", "").strip()
        raw_categories, tags = _parse_categories(outline.attrib.get("category", ""))

        feeds.append(
            FeedSpec(
                title=title,
                feed_url=feed_url,
                site_url=site_url,
                raw_categories=raw_categories,
                tags=tags,
            )
        )

    return feeds
