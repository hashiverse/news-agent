"""Pick the next article to post for a given identity.

Pure function. The caller passes:

- the identity's pool of candidate :class:`Article` (the union of every
  source's parsed entries),
- the cross-identity dedupe set of canonical URLs already posted in the
  last 24h,
- ``now_unix`` for the recency filter,
- a seeded :class:`random.Random` for the random pick.

Eligibility rules:

1. The article was published in the last 24 hours
   (``article.published_at_unix`` within ``[now - 24h, now]``).
   Articles with no publication date are conservatively skipped — we can't
   verify they're recent.
2. The article's canonical URL is NOT in the recently-posted set.

If multiple articles are eligible, one is chosen uniformly at random.
Returns ``None`` when nothing is eligible.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from news_agent.posts_db import ONE_DAY_SECONDS
from news_agent.rss_parser import Article


def pick_article(
    *,
    articles: Sequence[Article],
    recently_posted_canonical_urls: set[str],
    now_unix: int,
    rng: random.Random,
) -> Article | None:
    """Return one eligible article, or ``None`` if no candidate qualifies."""
    eligible = [
        article
        for article in articles
        if _is_eligible(article, recently_posted_canonical_urls, now_unix)
    ]
    if not eligible:
        return None
    return rng.choice(eligible)


def _is_eligible(
    article: Article, recently_posted: set[str], now_unix: int
) -> bool:
    if article.canonical_url in recently_posted:
        return False
    if article.published_at_unix is None:
        return False
    if article.published_at_unix < now_unix - ONE_DAY_SECONDS:
        return False
    if article.published_at_unix > now_unix + 60:
        # Slight tolerance for clock skew: future-dated more than 60s out is
        # almost certainly a feed bug. Skip.
        return False
    return True
