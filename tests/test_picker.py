"""Tests for picker.pick_article."""

from __future__ import annotations

import random

from news_agent.picker import pick_article
from news_agent.posts_db import ONE_DAY_SECONDS
from news_agent.rss_parser import Article


def _article(*, url: str, published_at: int | None) -> Article:
    return Article(
        title=f"t-{url}",
        canonical_url=url,
        raw_url=url,
        item_guid=None,
        summary="",
        published_at_unix=published_at,
        source_url="https://feed.example/rss",
    )


def test_picks_only_eligible_article():
    now = 10_000
    articles = [
        _article(url="https://x/a", published_at=now - 3600),  # eligible
        _article(url="https://x/old", published_at=now - 2 * ONE_DAY_SECONDS),  # too old
    ]
    chosen = pick_article(
        articles=articles,
        recently_posted_canonical_urls=set(),
        now_unix=now,
        rng=random.Random(0),
    )
    assert chosen is not None
    assert chosen.canonical_url == "https://x/a"


def test_excludes_already_posted_urls():
    now = 10_000
    articles = [
        _article(url="https://x/posted", published_at=now - 3600),
        _article(url="https://x/fresh", published_at=now - 1800),
    ]
    chosen = pick_article(
        articles=articles,
        recently_posted_canonical_urls={"https://x/posted"},
        now_unix=now,
        rng=random.Random(0),
    )
    assert chosen is not None
    assert chosen.canonical_url == "https://x/fresh"


def test_returns_none_when_nothing_eligible():
    now = 10_000
    articles = [
        _article(url="https://x/old", published_at=now - 2 * ONE_DAY_SECONDS),
        _article(url="https://x/posted", published_at=now - 3600),
    ]
    chosen = pick_article(
        articles=articles,
        recently_posted_canonical_urls={"https://x/posted"},
        now_unix=now,
        rng=random.Random(0),
    )
    assert chosen is None


def test_articles_with_no_pubdate_are_skipped():
    now = 10_000
    articles = [
        _article(url="https://x/no-date", published_at=None),
    ]
    chosen = pick_article(
        articles=articles,
        recently_posted_canonical_urls=set(),
        now_unix=now,
        rng=random.Random(0),
    )
    assert chosen is None


def test_far_future_articles_are_skipped():
    """Future-dated articles (clock skew or feed bug) are rejected."""
    now = 10_000
    articles = [
        _article(url="https://x/future", published_at=now + 3600),
    ]
    chosen = pick_article(
        articles=articles,
        recently_posted_canonical_urls=set(),
        now_unix=now,
        rng=random.Random(0),
    )
    assert chosen is None


def test_60s_clock_skew_tolerance():
    """A pubDate up to 60s in the future is still accepted."""
    now = 10_000
    articles = [
        _article(url="https://x/just-now", published_at=now + 30),
    ]
    chosen = pick_article(
        articles=articles,
        recently_posted_canonical_urls=set(),
        now_unix=now,
        rng=random.Random(0),
    )
    assert chosen is not None


def test_random_choice_is_uniform_across_eligible():
    """Across many seeded runs, all eligible articles are picked sometimes."""
    now = 10_000
    urls = [f"https://x/{i}" for i in range(5)]
    articles = [_article(url=u, published_at=now - 1800) for u in urls]
    seen: set[str] = set()
    for seed in range(100):
        chosen = pick_article(
            articles=articles,
            recently_posted_canonical_urls=set(),
            now_unix=now,
            rng=random.Random(seed),
        )
        assert chosen is not None
        seen.add(chosen.canonical_url)
    assert seen == set(urls)


def test_seeded_rng_is_deterministic():
    now = 10_000
    articles = [
        _article(url=f"https://x/{i}", published_at=now - 1800) for i in range(5)
    ]
    a = pick_article(
        articles=articles,
        recently_posted_canonical_urls=set(),
        now_unix=now,
        rng=random.Random(42),
    )
    b = pick_article(
        articles=articles,
        recently_posted_canonical_urls=set(),
        now_unix=now,
        rng=random.Random(42),
    )
    assert a is not None and b is not None
    assert a.canonical_url == b.canonical_url


def test_empty_articles_returns_none():
    chosen = pick_article(
        articles=[],
        recently_posted_canonical_urls=set(),
        now_unix=10_000,
        rng=random.Random(0),
    )
    assert chosen is None
