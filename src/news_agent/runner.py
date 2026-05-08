"""The main scheduling-and-posting loop.

On each iteration:

1. Snapshot the current identities (so config-file reloads are picked up
   automatically — :class:`runtime_state.RuntimeState` is mutated by the
   watcher's reload callback).
2. Compute every enabled identity's next-allowed-post time
   (:func:`scheduler.compute_next_post_time`).
3. Walk identities in soonest-first order. For each, fetch all its sources
   (:func:`rss_fetcher.fetch_feed_body`), parse them
   (:func:`rss_parser.parse_feed`), pool the articles, and ask the
   :func:`picker.pick_article` for an eligible candidate.
4. The first identity with an eligible article wins. Wait until that
   identity's scheduled time, then call :func:`posting.post_or_dry_run`.
5. If no identity has anything eligible, sleep ``NO_ELIGIBLE_POLL_SECONDS``
   and retry from step 1.

The sleep is always done via ``stop_event.wait(timeout=...)`` so shutdown
is prompt — no polling-loop hangs.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from collections.abc import Iterable

from news_agent.config import IdentityConfig
from news_agent.picker import pick_article
from news_agent.posting import post_or_dry_run
from news_agent.posts_db import (
    posted_canonical_urls_in_last_24h,
    posts_in_last_24h_for_identity,
)
from news_agent.rss_fetcher import FeedFetchError, fetch_feed_body
from news_agent.rss_parser import Article, parse_feed
from news_agent.runtime_state import RuntimeState
from news_agent.scheduler import compute_next_post_time

logger = logging.getLogger(__name__)

# When no identity has eligible content, sleep this long before re-checking.
# Short enough that a reload (control file edit) doesn't wait too long for
# the loop to notice the new config.
NO_ELIGIBLE_POLL_SECONDS = 60.0

# All stop_event.wait() calls are chunked into intervals this short. A single
# long Event.wait() can block prompt SIGINT/Ctrl-C delivery on Windows even
# in modern Python (we hit this in practice with 60s and 300s waits) — chunking
# guarantees the interpreter checks for pending signals within ~1s of Ctrl-C
# regardless of platform-specific signal-aware-wait behaviour. The overhead
# is one extra wakeup-per-second, which is negligible.
MAX_WAIT_INTERVAL_SECONDS = 1.0


def run_loop(
    *,
    state: RuntimeState,
    clients: dict[str, object],
    conn: sqlite3.Connection,
    stop_event: threading.Event,
    dry_run: bool,
    rng: random.Random | None = None,
) -> None:
    """Main scheduling loop. Returns when ``stop_event`` is set."""
    rng = rng if rng is not None else random.Random()
    logger.info("scheduling loop started (dry_run=%s)", dry_run)
    while not stop_event.is_set():
        try:
            _one_iteration(
                state=state,
                clients=clients,
                conn=conn,
                stop_event=stop_event,
                dry_run=dry_run,
                rng=rng,
            )
        except Exception:  # noqa: BLE001 — keep the loop alive across faults
            logger.exception("scheduling iteration raised; sleeping briefly and retrying")
            _interruptible_sleep(stop_event, 5.0)
    logger.info("scheduling loop stopped")


def _one_iteration(
    *,
    state: RuntimeState,
    clients: dict[str, object],
    conn: sqlite3.Connection,
    stop_event: threading.Event,
    dry_run: bool,
    rng: random.Random,
) -> None:
    snapshot = state.snapshot()
    identities = [i for i in snapshot.control.identities if i.enabled]
    if not identities:
        logger.debug("no enabled identities; sleeping %ds", int(NO_ELIGIBLE_POLL_SECONDS))
        _interruptible_sleep(stop_event, NO_ELIGIBLE_POLL_SECONDS)
        return

    now = int(time.time())
    scheduled = _build_schedule(identities, conn, now, rng)

    chosen = _find_soonest_with_eligible_content(
        scheduled=scheduled,
        clients=clients,
        conn=conn,
        now_unix=now,
        rng=rng,
    )
    if chosen is None:
        soonest_t, soonest_identity = scheduled[0]
        logger.info(
            "nothing eligible right now; next scheduled is %s at %s (in %s); will re-check in %ds",
            soonest_identity.log_label,
            _format_local_time(soonest_t),
            _format_duration(soonest_t - now),
            int(NO_ELIGIBLE_POLL_SECONDS),
        )
        _interruptible_sleep(stop_event, NO_ELIGIBLE_POLL_SECONDS)
        return

    next_post_time, identity, article = chosen

    seconds_until_post = next_post_time - int(time.time())
    if seconds_until_post > 1:
        # Announce the imminent post before sleeping; otherwise the loop is
        # silent for up to 24h between posts and the operator can't tell what
        # the daemon is "waiting for".
        logger.info(
            "next post: %s → %s at %s (in %s)",
            identity.log_label,
            _truncate_title(article.title),
            _format_local_time(next_post_time),
            _format_duration(seconds_until_post),
        )

    if not _wait_until(next_post_time, stop_event):
        # Stop event fired during the wait — bail out, the outer loop will exit.
        return

    client = clients.get(identity.salt)
    if client is None:
        # Could happen if the watcher reload removed this identity mid-wait.
        logger.info(
            "client for %s vanished during wait (likely a reload); next iteration",
            identity.log_label,
        )
        return

    try:
        post_or_dry_run(
            client=client,
            article=article,
            identity=identity,
            conn=conn,
            dry_run=dry_run,
            now_unix=int(time.time()),
        )
    except Exception:  # noqa: BLE001
        logger.exception("post failed for %s — continuing", identity.log_label)


def _build_schedule(
    identities: Iterable[IdentityConfig],
    conn: sqlite3.Connection,
    now_unix: int,
    rng: random.Random,
) -> list[tuple[int, IdentityConfig]]:
    schedule: list[tuple[int, IdentityConfig]] = []
    for identity in identities:
        recent = posts_in_last_24h_for_identity(conn, identity.salt, now_unix)
        next_t = compute_next_post_time(
            max_posts_per_day=identity.max_posts_per_day,
            posts_in_last_24h=recent,
            now_unix=now_unix,
            rng=rng,
        )
        schedule.append((next_t, identity))
    schedule.sort(key=lambda pair: pair[0])
    return schedule


def _find_soonest_with_eligible_content(
    *,
    scheduled: list[tuple[int, IdentityConfig]],
    clients: dict[str, object],
    conn: sqlite3.Connection,
    now_unix: int,
    rng: random.Random,
) -> tuple[int, IdentityConfig, Article] | None:
    recently_posted = posted_canonical_urls_in_last_24h(conn, now_unix)
    for next_t, identity in scheduled:
        if identity.salt not in clients:
            continue
        articles = _gather_articles_for_identity(identity, conn)
        chosen = pick_article(
            articles=articles,
            recently_posted_canonical_urls=recently_posted,
            now_unix=now_unix,
            rng=rng,
            keywords_required=identity.keywords_required,
            keywords_optional=identity.keywords_optional,
        )
        if chosen is not None:
            return next_t, identity, chosen
        logger.debug(
            "no eligible article for %s — trying next identity",
            identity.log_label,
        )
    return None


def _gather_articles_for_identity(
    identity: IdentityConfig, conn: sqlite3.Connection
) -> list[Article]:
    articles: list[Article] = []
    for source_url in identity.sources:
        try:
            body = fetch_feed_body(source_url, conn)
        except FeedFetchError as exc:
            logger.warning(
                "%s: feed fetch failed for %s: %s",
                identity.log_label,
                source_url,
                exc,
            )
            continue
        try:
            articles.extend(parse_feed(body, source_url))
        except Exception:  # noqa: BLE001
            logger.exception(
                "%s: feed parse failed for %s — skipping that source",
                identity.log_label,
                source_url,
            )
    return articles


def _format_local_time(unix_ts: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(unix_ts))


def _format_duration(seconds: int) -> str:
    """Format a non-negative duration as "Xs", "XmYYs", "XhYYm", or "XdYYh".

    Negative inputs clamp to zero (the schedule may have just slipped past now).
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m{s:02d}s"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h{m:02d}m"
    d, rem = divmod(seconds, 86400)
    h = rem // 3600
    return f"{d}d{h:02d}h"


def _truncate_title(title: str, max_len: int = 80) -> str:
    title = title.strip()
    if len(title) <= max_len:
        return title
    return title[: max_len - 1] + "…"


def _wait_until(target_unix: int, stop_event: threading.Event) -> bool:
    """Sleep in MAX_WAIT_INTERVAL_SECONDS chunks until ``target_unix``.

    Returns True if we made it to ``target_unix`` without stop_event firing,
    False if shutdown was signalled mid-wait.
    """
    while True:
        remaining = target_unix - int(time.time())
        if remaining <= 0:
            return True
        chunk = min(float(remaining), MAX_WAIT_INTERVAL_SECONDS)
        if stop_event.wait(timeout=chunk):
            return False


def _interruptible_sleep(stop_event: threading.Event, total_seconds: float) -> bool:
    """Sleep up to ``total_seconds``, checking ``stop_event`` in short chunks.

    Returns True if stop_event was set during the sleep, False if the full
    duration elapsed. Each underlying ``Event.wait()`` is capped at
    MAX_WAIT_INTERVAL_SECONDS so the Python interpreter regularly returns
    to bytecode and can deliver pending signals (e.g. Ctrl-C → KeyboardInterrupt).
    """
    deadline = time.monotonic() + total_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        chunk = min(remaining, MAX_WAIT_INTERVAL_SECONDS)
        if stop_event.wait(timeout=chunk):
            return True
