"""One-shot hashiverse connectivity smoke test.

Builds an ephemeral throwaway identity (random global salt, tempdir data
dir, cheap argon2), submits one OG-rich URL-preview post with a ``#test``
hashtag, and returns. All state is ephemeral — nothing persists past the
process.

Used by ``news-agent test-hashiverse`` to verify the hashiverse-client
plumbing works end-to-end against the production network without needing
any operator configuration (no control file, no ``NEWS_AGENT_GLOBAL_SALT``,
no identities required).
"""

from __future__ import annotations

import atexit
import logging
import secrets
import shutil
import tempfile
from collections.abc import Callable
from typing import Any

from hashiverse_client import HashiverseClient

from news_agent.keyphrase import derive_keyphrase_cheap
from news_agent.posting import format_post_html
from news_agent.rss_parser import Article
from news_agent.url_preview import UrlPreviewData, fetch_url_preview

logger = logging.getLogger(__name__)

# Fixed real article with rich OG metadata — gives the test post a fully
# rendered preview card (title, description, image) so the operator can see
# what a production post actually looks like end-to-end.
TEST_POST_URL = (
    "https://www.techradar.com/pro/"
    "digital-sovereignty-is-no-longer-a-policy-debate-"
    "its-technology-decision"
)


def run_hashiverse_smoke_test(
    *,
    client_factory: Callable[..., Any] = HashiverseClient.create_from_keyphrase,
    preview_fn: Callable[[str], UrlPreviewData] = fetch_url_preview,
) -> str:
    """Build an ephemeral client, submit one OG-rich test post, return the URL.

    Returns the URL embedded in the post so callers (and tests) can assert
    what was submitted. ``client_factory`` and ``preview_fn`` are injected
    so unit tests can substitute fakes without monkey-patching.
    """
    global_salt = secrets.token_hex(32)
    local_salt = secrets.token_hex(32)

    data_dir = tempfile.mkdtemp(prefix="news-agent-test-hashiverse-")
    atexit.register(shutil.rmtree, data_dir, ignore_errors=True)
    logger.info("ephemeral data dir: %s", data_dir)

    keyphrase = derive_keyphrase_cheap(global_salt, local_salt)

    logger.info("constructing ephemeral hashiverse client (DNSSEC bootstrap)")
    client = client_factory(
        key_phrase=keyphrase,
        data_dir=data_dir,
        passphrase=global_salt,
        bootstrap_addresses=None,
    )
    logger.info("ephemeral client up: client_id=%s", client.client_id)

    logger.info("fetching OG metadata from %s", TEST_POST_URL)
    preview = preview_fn(TEST_POST_URL)

    # Build the post body via the exact production formatter so the smoke
    # post is byte-identical to what a real news-agent post pointing at
    # this URL with `hashtags: [test]` would produce. The synthetic Article
    # is a wrapper to satisfy the function signature: all visible content
    # comes from `preview` (its OG title/description/image take precedence
    # over the article's blank fields, per format_post_html's fallback chain).
    synthetic_article = Article(
        title="",
        canonical_url=TEST_POST_URL,
        raw_url=TEST_POST_URL,
        item_guid=None,
        summary="",
        published_at_unix=None,
        source_url="",
    )
    body = format_post_html(synthetic_article, preview, hashtags=("test",))
    url = preview.url or TEST_POST_URL

    logger.info(
        "submitting test post: title=%r url=%s #test",
        preview.title or TEST_POST_URL, url,
    )
    # `submit_post` is synchronous from Python's perspective: the underlying
    # Rust future awaits the commit to at least one User bucket before
    # returning (see hashiverse_client.rs:292-294 — "Failed to post to any
    # User buckets, so bailing"). So by the time this call returns the post
    # is already durably committed; no extra readback is needed.
    client.submit_post(body)
    logger.info("submit_post returned (post committed to at least one User bucket)")

    # Explicit drop so the Rust destructor (which blocks on Rust-side cleanup,
    # including any background tasks) runs before this function returns.
    del client
    return url
