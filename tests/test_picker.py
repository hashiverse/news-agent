"""Tests for picker.pick_article."""

from __future__ import annotations

import random

from news_agent.picker import pick_article
from news_agent.posts_db import ONE_DAY_SECONDS
from news_agent.rss_parser import Article


def _article(*, url: str, published_at: int | None, title: str | None = None, summary: str = "") -> Article:
    return Article(
        title=title if title is not None else f"t-{url}",
        canonical_url=url,
        raw_url=url,
        item_guid=None,
        summary=summary,
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


# ---------------------------------------------------------------------------
# keywords_required (AND) and keywords_optional (OR)


def _fresh_article(*, url: str, title: str = "", summary: str = "") -> Article:
    """Helper for keyword tests — the article is recent and fresh enough."""
    return _article(url=url, published_at=10_000 - 1800, title=title, summary=summary)


def _pick(articles, *, keywords_required=(), keywords_optional=()):
    return pick_article(
        articles=articles,
        recently_posted_canonical_urls=set(),
        now_unix=10_000,
        rng=random.Random(0),
        keywords_required=keywords_required,
        keywords_optional=keywords_optional,
    )


def test_no_keywords_means_no_filter():
    """The default empty filters preserve the legacy behaviour."""
    article = _fresh_article(url="https://x/a", title="Anything goes")
    assert _pick([article]) is article


def test_keywords_required_all_must_match_in_title_or_summary():
    a = _fresh_article(url="https://x/a", title="Rust async news", summary="")
    b = _fresh_article(url="https://x/b", title="Rust news", summary="")
    c = _fresh_article(url="https://x/c", title="Generic news", summary="rust async details")
    # Only `a` and `c` carry both "rust" AND "async" in title-or-summary.
    eligible_urls = set()
    for seed in range(40):
        chosen = pick_article(
            articles=[a, b, c],
            recently_posted_canonical_urls=set(),
            now_unix=10_000,
            rng=random.Random(seed),
            keywords_required=("rust", "async"),
        )
        assert chosen is not None
        eligible_urls.add(chosen.canonical_url)
    assert eligible_urls == {"https://x/a", "https://x/c"}


def test_keywords_required_no_match_returns_none():
    a = _fresh_article(url="https://x/a", title="Cooking with cheese", summary="")
    assert _pick([a], keywords_required=("rust",)) is None


def test_keywords_optional_any_match_is_enough():
    a = _fresh_article(url="https://x/a", title="A piece on rust", summary="")
    b = _fresh_article(url="https://x/b", title="Cooking with cheese", summary="")
    c = _fresh_article(url="https://x/c", title="WASM speedups", summary="")
    eligible = set()
    for seed in range(40):
        chosen = pick_article(
            articles=[a, b, c],
            recently_posted_canonical_urls=set(),
            now_unix=10_000,
            rng=random.Random(seed),
            keywords_optional=("rust", "wasm"),
        )
        assert chosen is not None
        eligible.add(chosen.canonical_url)
    assert eligible == {"https://x/a", "https://x/c"}


def test_keywords_optional_none_match_returns_none():
    a = _fresh_article(url="https://x/a", title="Cooking with cheese")
    assert _pick([a], keywords_optional=("rust", "wasm")) is None


def test_required_and_optional_combined():
    """All required must match AND at least one optional must match."""
    # Required: rust. Optional: async OR threading.
    yes = _fresh_article(url="https://x/yes", title="Rust async patterns", summary="")
    missing_required = _fresh_article(url="https://x/no1", title="Async in Go", summary="")
    missing_optional = _fresh_article(url="https://x/no2", title="Rust beginner guide", summary="")
    both = _fresh_article(url="https://x/both", title="Rust threading deep dive", summary="")
    eligible = set()
    for seed in range(40):
        chosen = pick_article(
            articles=[yes, missing_required, missing_optional, both],
            recently_posted_canonical_urls=set(),
            now_unix=10_000,
            rng=random.Random(seed),
            keywords_required=("rust",),
            keywords_optional=("async", "threading"),
        )
        assert chosen is not None
        eligible.add(chosen.canonical_url)
    assert eligible == {"https://x/yes", "https://x/both"}


def test_keywords_match_is_case_insensitive():
    a = _fresh_article(url="https://x/a", title="RUST in production")
    chosen = _pick([a], keywords_required=("rust",))
    assert chosen is a


def test_keywords_match_against_summary():
    a = _fresh_article(url="https://x/a", title="Generic headline", summary="A long discussion of Rust internals.")
    chosen = _pick([a], keywords_required=("rust",))
    assert chosen is a


def test_keyword_substring_does_match_within_word():
    """Pure substring match — `rust` matches `trust` because the user explicitly
    asked for substring filtering. Document the behaviour so it can't regress."""
    a = _fresh_article(url="https://x/a", title="Building trust in distributed systems")
    chosen = _pick([a], keywords_required=("rust",))
    assert chosen is a
