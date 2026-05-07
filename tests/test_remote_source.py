"""Tests for remote_source — URL classification, GitHub URL rewriting, conditional GET, cache writes."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from news_agent.remote_source import (
    CachedFile,
    FetchOutcome,
    fetch_to_cache,
    is_url,
    normalize_github_url,
)

# ---------------------------------------------------------------------------
# URL classification + normalization
# ---------------------------------------------------------------------------


def test_is_url_true_for_http_and_https():
    assert is_url("http://example.com/x")
    assert is_url("https://example.com/x")


def test_is_url_false_for_paths():
    assert not is_url("./feeds.opml")
    assert not is_url("/etc/news-agent/feeds.opml")
    assert not is_url("D:\\Repos\\hashiverse\\news-agent\\example\\feeds.opml")
    assert not is_url("file:///tmp/x")


def test_normalize_github_blob_to_raw():
    blob = "https://github.com/hashiverse/news-feeds/blob/main/feeds.opml"
    raw = "https://raw.githubusercontent.com/hashiverse/news-feeds/main/feeds.opml"
    assert normalize_github_url(blob) == raw


def test_normalize_github_blob_with_nested_path():
    blob = "https://github.com/owner/repo/blob/main/dir/sub/file.yaml"
    raw = "https://raw.githubusercontent.com/owner/repo/main/dir/sub/file.yaml"
    assert normalize_github_url(blob) == raw


def test_normalize_already_raw_url_unchanged():
    raw = "https://raw.githubusercontent.com/hashiverse/news-feeds/main/feeds.opml"
    assert normalize_github_url(raw) == raw


def test_normalize_unrelated_url_unchanged():
    other = "https://example.com/feeds.opml"
    assert normalize_github_url(other) == other


def test_normalize_github_repo_root_unchanged():
    # No /blob/ segment — leave alone.
    repo = "https://github.com/hashiverse/news-feeds"
    assert normalize_github_url(repo) == repo


# ---------------------------------------------------------------------------
# fetch_to_cache against a live in-process HTTP server
# ---------------------------------------------------------------------------


HandlerFactory = Callable[[], type[BaseHTTPRequestHandler]]


@contextmanager
def _running_server(handler_cls: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    """Spin up a ThreadingHTTPServer on localhost; yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _make_handler(
    body: bytes = b"hello",
    etag: str | None = '"v1"',
    last_modified: str | None = None,
    status_for_conditional: int = 304,
    error_status: int | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build a one-shot handler class that records the requests it received."""

    received_headers: list[dict[str, str]] = []
    request_count = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        # Quiet the noisy default per-request logging.
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            request_count["n"] += 1
            received_headers.append({k: v for k, v in self.headers.items()})
            if error_status is not None:
                self.send_response(error_status)
                self.end_headers()
                return
            inm = self.headers.get("If-None-Match")
            ims = self.headers.get("If-Modified-Since")
            if (etag and inm == etag) or (last_modified and ims == last_modified):
                self.send_response(status_for_conditional)
                self.end_headers()
                return
            self.send_response(200)
            if etag:
                self.send_header("ETag", etag)
            if last_modified:
                self.send_header("Last-Modified", last_modified)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    Handler.received_headers = received_headers  # type: ignore[attr-defined]
    Handler.request_count = request_count  # type: ignore[attr-defined]
    return Handler


def _cached(tmp_path: Path) -> CachedFile:
    return CachedFile.for_filename(tmp_path, "data.bin")


def test_first_fetch_writes_body_and_sidecar(tmp_path):
    handler = _make_handler(body=b"opml content here", etag='"abc"')
    cached = _cached(tmp_path)

    with _running_server(handler) as base:
        outcome = fetch_to_cache(f"{base}/feeds.opml", cached)

    assert outcome is FetchOutcome.UPDATED
    assert cached.path.read_bytes() == b"opml content here"
    meta = json.loads(cached.meta_path.read_text())
    assert meta["etag"] == '"abc"'
    assert meta["url"].endswith("/feeds.opml")
    assert "fetched_at" in meta


def test_second_fetch_with_etag_returns_not_modified(tmp_path):
    handler = _make_handler(body=b"x", etag='"abc"')
    cached = _cached(tmp_path)

    with _running_server(handler) as base:
        first = fetch_to_cache(f"{base}/feeds.opml", cached)
        # Manipulate the cache file's timestamp / content to detect untouched-ness.
        original_bytes = cached.path.read_bytes()
        second = fetch_to_cache(f"{base}/feeds.opml", cached)

    assert first is FetchOutcome.UPDATED
    assert second is FetchOutcome.NOT_MODIFIED
    assert cached.path.read_bytes() == original_bytes
    # Server must have received If-None-Match on the second request.
    inm_seen = handler.received_headers[1].get("If-None-Match")  # type: ignore[attr-defined]
    assert inm_seen == '"abc"'


def test_fetch_uses_stored_last_modified_when_no_etag(tmp_path):
    handler = _make_handler(body=b"x", etag=None, last_modified="Wed, 01 Jan 2025 00:00:00 GMT")
    cached = _cached(tmp_path)

    with _running_server(handler) as base:
        fetch_to_cache(f"{base}/feeds.opml", cached)
        fetch_to_cache(f"{base}/feeds.opml", cached)

    ims_seen = handler.received_headers[1].get("If-Modified-Since")  # type: ignore[attr-defined]
    assert ims_seen == "Wed, 01 Jan 2025 00:00:00 GMT"


def test_500_with_existing_cache_returns_stale(tmp_path):
    cached = _cached(tmp_path)
    cached.path.write_bytes(b"stale-but-valid")
    cached.meta_path.write_text(json.dumps({"etag": '"old"'}))
    handler = _make_handler(error_status=500)

    with _running_server(handler) as base:
        outcome = fetch_to_cache(f"{base}/feeds.opml", cached)

    assert outcome is FetchOutcome.STALE
    assert cached.path.read_bytes() == b"stale-but-valid"


def test_500_without_cache_returns_no_cache(tmp_path):
    cached = _cached(tmp_path)
    handler = _make_handler(error_status=500)

    with _running_server(handler) as base:
        outcome = fetch_to_cache(f"{base}/feeds.opml", cached)

    assert outcome is FetchOutcome.NO_CACHE
    assert not cached.path.exists()


def test_404_with_existing_cache_returns_stale(tmp_path):
    cached = _cached(tmp_path)
    cached.path.write_bytes(b"old")
    cached.meta_path.write_text(json.dumps({}))
    handler = _make_handler(error_status=404)

    with _running_server(handler) as base:
        outcome = fetch_to_cache(f"{base}/feeds.opml", cached)

    assert outcome is FetchOutcome.STALE
    assert cached.path.read_bytes() == b"old"


def test_unreachable_url_with_no_cache_returns_no_cache(tmp_path):
    cached = _cached(tmp_path)
    # Port 1 is reserved and won't accept connections.
    outcome = fetch_to_cache("http://127.0.0.1:1/never", cached, timeout=1)
    assert outcome is FetchOutcome.NO_CACHE


def test_corrupt_meta_treated_as_missing(tmp_path):
    """A garbled .meta.json file should be silently re-built on next 200."""
    cached = _cached(tmp_path)
    cached.meta_path.write_text("not-json")
    handler = _make_handler(body=b"new", etag='"v1"')

    with _running_server(handler) as base:
        outcome = fetch_to_cache(f"{base}/feeds.opml", cached)

    assert outcome is FetchOutcome.UPDATED
    meta = json.loads(cached.meta_path.read_text())
    assert meta["etag"] == '"v1"'


def test_no_tmp_files_left_behind_on_success(tmp_path):
    cached = _cached(tmp_path)
    handler = _make_handler(body=b"x")
    with _running_server(handler) as base:
        fetch_to_cache(f"{base}/feeds.opml", cached)
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert all(not name.endswith(".tmp") for name in siblings), siblings
