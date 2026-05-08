"""Pick the next article to post for a given identity.

Pure function. The caller passes:

- the identity's pool of candidate :class:`Article` (the union of every
  source's parsed entries),
- the cross-identity dedupe set of canonical URLs already posted in the
  last 24h,
- ``now_unix`` for the recency filter,
- a seeded :class:`random.Random` for the random pick,
- optional keyword filters (``keywords_required`` / ``keywords_optional``)
  that are case-insensitive substring matches against the title plus the
  full summary, with HTML and ``#hashtag`` tokens stripped first.

Eligibility rules:

1. The article was published in the last 24 hours
   (``article.published_at_unix`` within ``[now - 24h, now]``).
   Articles with no publication date are conservatively skipped — we can't
   verify they're recent.
2. The article's canonical URL is NOT in the recently-posted set.
3. If ``keywords_required`` is non-empty, ALL of its entries must appear
   somewhere in the cleaned haystack (case-insensitive substring).
4. If ``keywords_optional`` is non-empty, AT LEAST ONE of its entries must
   appear in the cleaned haystack. Either filter being empty means that
   check is skipped.

The haystack is built as ``title + " " + strip_html(summary)``, then run
through ``strip_hashtags`` to remove SEO/hashtag dumps that would otherwise
cause false positives — common on YouTube descriptions, e.g.
``... #robotaxi #Tesla #FSD #autonomousdriving``. Plain words in the
narrative text still match normally.

If multiple articles are eligible, one is chosen uniformly at random.
Returns ``None`` when nothing is eligible.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence

from news_agent.posts_db import ONE_DAY_SECONDS
from news_agent.rss_parser import Article
from news_agent.text_utils import strip_hashtags, strip_html

logger = logging.getLogger(__name__)

# Truncate the haystack in log lines so long lead sentences don't dominate
# the operator's terminal.
_HAYSTACK_LOG_CAP = 240

# Process-wide flag set by `cli.run` from the `--verbose-filtering` CLI
# option. Off by default — the picker rejects most articles in steady state
# (recency / dedupe / keyword), so per-rejection INFO logging would flood
# stderr. Operators flip it on when tuning a `keywords_required` /
# `keywords_optional` config and want to see exactly what's getting filtered.
_VERBOSE_FILTERING = False


def set_verbose_filtering(enabled: bool) -> None:
    """Toggle INFO-level logging for keyword-filter rejections.

    Process-wide one-shot, called by `cli.run`. See `_VERBOSE_FILTERING`.
    """
    global _VERBOSE_FILTERING
    _VERBOSE_FILTERING = enabled


def pick_article(
    *,
    articles: Sequence[Article],
    recently_posted_canonical_urls: set[str],
    now_unix: int,
    rng: random.Random,
    keywords_required: Sequence[str] = (),
    keywords_optional: Sequence[str] = (),
) -> Article | None:
    """Return one eligible article, or ``None`` if no candidate qualifies.

    ``keywords_required`` / ``keywords_optional`` should be lower-cased by
    the caller (see ``IdentityConfig``); the haystack is also lower-cased
    before substring matching.
    """
    eligible = [
        article
        for article in articles
        if _is_eligible(
            article,
            recently_posted_canonical_urls,
            now_unix,
            keywords_required,
            keywords_optional,
        )
    ]
    if not eligible:
        return None
    return rng.choice(eligible)


def _is_eligible(
    article: Article,
    recently_posted: set[str],
    now_unix: int,
    keywords_required: Sequence[str],
    keywords_optional: Sequence[str],
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
    if keywords_required or keywords_optional:
        # Strip HTML so we match the visible text only, then strip `#tag`
        # tokens so SEO/hashtag dumps (`... #tesla #robotaxi #FSD`) can't
        # cause false positives. Plain words still match normally.
        summary_clean = strip_html(article.summary)
        haystack = strip_hashtags(f"{article.title} {summary_clean}").lower()
        if keywords_required:
            missing = [kw for kw in keywords_required if kw not in haystack]
            if missing:
                if _VERBOSE_FILTERING:
                    logger.info(
                        "keyword filter rejected %r: required %s missing; haystack=%r",
                        article.title,
                        missing,
                        _truncate_for_log(haystack),
                    )
                return False
        if keywords_optional and not any(kw in haystack for kw in keywords_optional):
            if _VERBOSE_FILTERING:
                logger.info(
                    "keyword filter rejected %r: no optional keyword in %s matched; haystack=%r",
                    article.title,
                    list(keywords_optional),
                    _truncate_for_log(haystack),
                )
            return False
    return True


def _truncate_for_log(text: str) -> str:
    if len(text) <= _HAYSTACK_LOG_CAP:
        return text
    return text[: _HAYSTACK_LOG_CAP - 1] + "…"
