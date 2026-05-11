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
from dataclasses import dataclass
from enum import StrEnum

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


class RejectionReason(StrEnum):
    """First failing eligibility check for an article."""

    DEDUPE = "dedupe"
    NO_PUBLISH_DATE = "no_publish_date"
    STALE = "stale"
    FUTURE_DATED = "future_dated"
    KEYWORDS_REQUIRED = "keywords_required"
    KEYWORDS_OPTIONAL = "keywords_optional"


@dataclass(frozen=True)
class PickerCounts:
    """How many candidates were rejected for each reason.

    Per-article exclusive: each article counts in exactly one bucket (the
    first failing check). ``sum(rejected_*) + eligible == total_candidates``.
    """

    total_candidates: int
    rejected_dedupe: int
    rejected_no_publish_date: int
    rejected_stale: int
    rejected_future_dated: int
    rejected_keywords_required: int
    rejected_keywords_optional: int
    eligible: int


@dataclass(frozen=True)
class PickerResult:
    """Outcome of one ``pick_article`` call: optional chosen article plus
    rejection counters the caller can log for diagnosability."""

    chosen: Article | None
    counts: PickerCounts


def pick_article(
    *,
    articles: Sequence[Article],
    recently_posted_canonical_urls: set[str],
    now_unix: int,
    rng: random.Random,
    keywords_required: Sequence[str] = (),
    keywords_optional: Sequence[str] = (),
) -> PickerResult:
    """Return a :class:`PickerResult` carrying one eligible article (or
    ``None``) plus per-reason rejection counts.

    ``keywords_required`` / ``keywords_optional`` should be lower-cased by
    the caller (see ``IdentityConfig``); the haystack is also lower-cased
    before substring matching.
    """
    eligible: list[Article] = []
    tally = {reason: 0 for reason in RejectionReason}
    for article in articles:
        reason = _classify(
            article,
            recently_posted_canonical_urls,
            now_unix,
            keywords_required,
            keywords_optional,
        )
        if reason is None:
            eligible.append(article)
        else:
            tally[reason] += 1

    counts = PickerCounts(
        total_candidates=len(articles),
        rejected_dedupe=tally[RejectionReason.DEDUPE],
        rejected_no_publish_date=tally[RejectionReason.NO_PUBLISH_DATE],
        rejected_stale=tally[RejectionReason.STALE],
        rejected_future_dated=tally[RejectionReason.FUTURE_DATED],
        rejected_keywords_required=tally[RejectionReason.KEYWORDS_REQUIRED],
        rejected_keywords_optional=tally[RejectionReason.KEYWORDS_OPTIONAL],
        eligible=len(eligible),
    )
    chosen = rng.choice(eligible) if eligible else None
    return PickerResult(chosen=chosen, counts=counts)


def _classify(
    article: Article,
    recently_posted: set[str],
    now_unix: int,
    keywords_required: Sequence[str],
    keywords_optional: Sequence[str],
) -> RejectionReason | None:
    """Return the first failing eligibility reason, or ``None`` if eligible.

    Check order is significant: a single article fails at most one reason,
    and ``pick_article`` tallies that reason into its counter. Tests in
    ``test_picker.py`` pin this exclusive-bucket behaviour.
    """
    if article.canonical_url in recently_posted:
        return RejectionReason.DEDUPE
    if article.published_at_unix is None:
        return RejectionReason.NO_PUBLISH_DATE
    if article.published_at_unix < now_unix - ONE_DAY_SECONDS:
        return RejectionReason.STALE
    if article.published_at_unix > now_unix + 60:
        # Slight tolerance for clock skew: future-dated more than 60s out is
        # almost certainly a feed bug. Skip.
        return RejectionReason.FUTURE_DATED
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
                return RejectionReason.KEYWORDS_REQUIRED
        if keywords_optional and not any(kw in haystack for kw in keywords_optional):
            if _VERBOSE_FILTERING:
                logger.info(
                    "keyword filter rejected %r: no optional keyword in %s matched; haystack=%r",
                    article.title,
                    list(keywords_optional),
                    _truncate_for_log(haystack),
                )
            return RejectionReason.KEYWORDS_OPTIONAL
    return None


def _truncate_for_log(text: str) -> str:
    if len(text) <= _HAYSTACK_LOG_CAP:
        return text
    return text[: _HAYSTACK_LOG_CAP - 1] + "…"
