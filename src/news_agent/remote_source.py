"""HTTPS-fetched feed/control files with on-disk cache and conditional GET.

When ``--feeds`` or ``--control`` is given as an HTTPS URL, the daemon fetches
the file once at startup, caches it under the per-daemon cache directory, and
hands the cache path to the existing pipeline (so :mod:`opml_loader`,
:mod:`config`, and :mod:`watcher` are unchanged).

A background poller (see :mod:`news_agent.poller`) calls :func:`fetch_to_cache`
on a schedule. Each call performs a conditional GET using the previously-stored
ETag and Last-Modified headers — bandwidth-friendly, plays nicely with GitHub.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

GITHUB_BLOB_PREFIX = "https://github.com/"
GITHUB_RAW_PREFIX = "https://raw.githubusercontent.com/"

USER_AGENT = "news-agent/0.1 (+https://github.com/hashiverse/news-agent)"
DEFAULT_TIMEOUT_SECONDS = 30.0


def is_url(value: str) -> bool:
    """True if ``value`` looks like an HTTP(S) URL."""
    return value.startswith(("http://", "https://"))


def normalize_github_url(url: str) -> str:
    """Rewrite ``github.com/<o>/<r>/blob/<branch>/<path>`` to the raw URL.

    The ``blob/`` page is HTML-rendered, not the file body. Operators commonly
    paste blob URLs from their browser; we silently fix it.

    Limitation: branch names containing slashes are not handled here — the
    parser cannot disambiguate them without a GitHub API call. Operators with
    slash-containing branches should use the explicit raw URL form.
    """
    if not url.startswith(GITHUB_BLOB_PREFIX):
        return url
    rest = url[len(GITHUB_BLOB_PREFIX):]
    parts = rest.split("/", 4)
    if len(parts) < 5 or parts[2] != "blob":
        return url
    owner, repo, _, branch, path = parts
    return f"{GITHUB_RAW_PREFIX}{owner}/{repo}/{branch}/{path}"


class FetchOutcome(Enum):
    """Result of a single :func:`fetch_to_cache` call."""

    UPDATED = "updated"           # 200 OK, cache rewritten with new content
    NOT_MODIFIED = "not_modified"  # 304, cache untouched
    STALE = "stale"               # error, cache exists, kept
    NO_CACHE = "no_cache"         # error, no cache available — caller decides what to do


@dataclass(frozen=True)
class CachedFile:
    """A pair of (content, sidecar) paths for one cached remote file."""

    path: Path
    meta_path: Path

    @classmethod
    def for_filename(cls, cache_dir: Path, filename: str) -> "CachedFile":
        return cls(
            path=cache_dir / filename,
            meta_path=cache_dir / f"{filename}.meta.json",
        )


def _read_meta(meta_path: Path) -> dict:
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Corrupt sidecar — discard so we re-fetch unconditionally.
        return {}


def _atomic_write_bytes(path: Path, body: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def fetch_to_cache(
    url: str,
    cached: CachedFile,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: urllib.request.OpenerDirector | None = None,
) -> FetchOutcome:
    """Issue a conditional GET to ``url`` and update the cache.

    On 200 OK the cache content + sidecar are atomically rewritten. On 304 the
    sidecar's ``fetched_at`` is bumped but the content is left alone. On other
    errors, an existing cache is preserved (returning :attr:`FetchOutcome.STALE`),
    or :attr:`FetchOutcome.NO_CACHE` is returned if there's nothing to fall back to.
    """
    meta = _read_meta(cached.meta_path)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if etag := meta.get("etag"):
        request.add_header("If-None-Match", etag)
    if last_modified := meta.get("last_modified"):
        request.add_header("If-Modified-Since", last_modified)

    open_func = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_func(request, timeout=timeout) as response:
            body = response.read()
            new_meta = {
                "url": url,
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "fetched_at": time.time(),
            }
            _atomic_write_bytes(cached.path, body)
            _atomic_write_json(cached.meta_path, new_meta)
            logger.info("fetched %s (%d bytes) → %s", url, len(body), cached.path)
            return FetchOutcome.UPDATED
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            new_meta = dict(meta)
            new_meta["fetched_at"] = time.time()
            _atomic_write_json(cached.meta_path, new_meta)
            logger.debug("304 not modified: %s", url)
            return FetchOutcome.NOT_MODIFIED
        if cached.path.exists():
            logger.warning("HTTP %d fetching %s — using stale cache at %s", exc.code, url, cached.path)
            return FetchOutcome.STALE
        logger.error("HTTP %d fetching %s and no cache available", exc.code, url)
        return FetchOutcome.NO_CACHE
    except (urllib.error.URLError, OSError) as exc:
        if cached.path.exists():
            logger.warning("network error fetching %s (%s) — using stale cache at %s", url, exc, cached.path)
            return FetchOutcome.STALE
        logger.error("network error fetching %s (%s) and no cache available", url, exc)
        return FetchOutcome.NO_CACHE
