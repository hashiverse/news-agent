"""Tests for news_agent.url_preview — local OG fetch + extraction."""

from __future__ import annotations

import threading
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from news_agent.url_preview import (
    MAX_BODY_BYTES,
    UrlPreviewData,
    UrlPreviewError,
    _extract_url_preview,
    _fetch_html,
    fetch_url_preview,
)


# ---------------------------------------------------------------------------
# _extract_url_preview — pure parsing tests, no network
# ---------------------------------------------------------------------------


def test_extract_full_og_block():
    html = """
    <html><head>
      <meta property="og:title" content="OG Title">
      <meta property="og:description" content="OG description text">
      <meta property="og:image" content="https://img.example/og.png">
      <meta property="og:url" content="https://example.com/canonical">
      <title>Page Title</title>
    </head><body>x</body></html>
    """
    out = _extract_url_preview(html, fetched_from_url="https://input/")
    assert out == UrlPreviewData(
        url="https://example.com/canonical",
        title="OG Title",
        description="OG description text",
        image_url="https://img.example/og.png",
    )


def test_extract_falls_back_to_twitter_card_when_og_missing():
    html = """
    <html><head>
      <meta name="twitter:title" content="Twitter Title">
      <meta name="twitter:description" content="Twitter description">
      <meta name="twitter:image" content="https://img.example/tw.png">
      <title>Page Title</title>
    </head></html>
    """
    out = _extract_url_preview(html, fetched_from_url="https://input/")
    assert out.title == "Twitter Title"
    assert out.description == "Twitter description"
    assert out.image_url == "https://img.example/tw.png"
    # No og:url, no canonical link → echoes back input.
    assert out.url == "https://input/"


def test_extract_falls_back_to_title_meta_description_and_canonical_link():
    html = """
    <html><head>
      <title>Page Title</title>
      <meta name="description" content="Plain meta description">
      <link rel="canonical" href="https://example.com/canonical-link">
    </head></html>
    """
    out = _extract_url_preview(html, fetched_from_url="https://input/")
    assert out.title == "Page Title"
    assert out.description == "Plain meta description"
    assert out.image_url == ""
    assert out.url == "https://example.com/canonical-link"


def test_extract_no_metadata_at_all_echoes_input_url_only():
    html = "<html><head></head><body>nothing</body></html>"
    out = _extract_url_preview(html, fetched_from_url="https://input/")
    assert out == UrlPreviewData(url="https://input/")


def test_extract_image_falls_through_og_to_twitter_image_to_twitter_image_src():
    html_with_only_image_src = """
    <html><head>
      <meta name="twitter:image:src" content="https://img.example/src.png">
    </head></html>
    """
    out = _extract_url_preview(html_with_only_image_src, fetched_from_url="https://input/")
    assert out.image_url == "https://img.example/src.png"

    html_with_twitter_image = """
    <html><head>
      <meta name="twitter:image" content="https://img.example/tw.png">
      <meta name="twitter:image:src" content="https://img.example/src.png">
    </head></html>
    """
    out = _extract_url_preview(html_with_twitter_image, fetched_from_url="https://input/")
    assert out.image_url == "https://img.example/tw.png"

    html_with_og = """
    <html><head>
      <meta property="og:image" content="https://img.example/og.png">
      <meta name="twitter:image" content="https://img.example/tw.png">
      <meta name="twitter:image:src" content="https://img.example/src.png">
    </head></html>
    """
    out = _extract_url_preview(html_with_og, fetched_from_url="https://input/")
    assert out.image_url == "https://img.example/og.png"


def test_extract_canonical_link_reads_href_not_content():
    """`<link rel="canonical">` carries the URL in `href`, not `content`."""
    html = """
    <html><head>
      <link rel="canonical" href="https://example.com/correct" content="https://example.com/wrong">
    </head></html>
    """
    out = _extract_url_preview(html, fetched_from_url="https://input/")
    assert out.url == "https://example.com/correct"


def test_extract_decodes_html_entities_in_attributes():
    html = """
    <html><head>
      <meta property="og:title" content="Cats &amp; Dogs &#x27;n&#x27; Other Beasts">
    </head></html>
    """
    out = _extract_url_preview(html, fetched_from_url="https://input/")
    assert out.title == "Cats & Dogs 'n' Other Beasts"


def test_extract_first_og_title_wins_when_duplicates():
    html = """
    <html><head>
      <meta property="og:title" content="First">
      <meta property="og:title" content="Second">
    </head></html>
    """
    out = _extract_url_preview(html, fetched_from_url="https://input/")
    assert out.title == "First"


def test_extract_ignores_body_after_head_close():
    """Sanity-check: tags inside <body> must not influence extraction."""
    html = """
    <html><head>
      <meta property="og:title" content="Real Title">
    </head><body>
      <meta property="og:title" content="Should Not Win">
      <title>Should Not Win Either</title>
    </body></html>
    """
    out = _extract_url_preview(html, fetched_from_url="https://input/")
    assert out.title == "Real Title"


def test_extract_strips_whitespace_around_title_text():
    html = "<html><head><title>\n  Trimmed Title  \n</title></head></html>"
    out = _extract_url_preview(html, fetched_from_url="https://input/")
    assert out.title == "Trimmed Title"


# ---------------------------------------------------------------------------
# _fetch_html — local test server
# ---------------------------------------------------------------------------


@contextmanager
def _running_server(handler_cls: type[BaseHTTPRequestHandler]) -> Iterator[str]:
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


def _make_handler(body: bytes, *, content_type: str = "text/html; charset=utf-8", status: int = 200):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def test_fetch_html_200_returns_decoded_body():
    handler = _make_handler(b"<html><head><title>OK</title></head></html>")
    with _running_server(handler) as base:
        text = _fetch_html(f"{base}/page.html")
    assert "<title>OK</title>" in text


def test_fetch_html_respects_charset_in_content_type():
    body = "<title>café</title>".encode("iso-8859-1")
    handler = _make_handler(body, content_type="text/html; charset=ISO-8859-1")
    with _running_server(handler) as base:
        text = _fetch_html(f"{base}/page.html")
    assert "café" in text


def test_fetch_html_falls_back_to_utf8_when_charset_missing():
    body = "<title>héllo</title>".encode("utf-8")
    handler = _make_handler(body, content_type="text/html")
    with _running_server(handler) as base:
        text = _fetch_html(f"{base}/page.html")
    assert "héllo" in text


def test_fetch_html_falls_back_to_utf8_replace_on_unknown_charset():
    body = b"<title>x</title>"
    handler = _make_handler(body, content_type="text/html; charset=banana")
    with _running_server(handler) as base:
        # Should not raise; LookupError caught internally and falls back.
        text = _fetch_html(f"{base}/page.html")
    assert "<title>x</title>" in text


def test_fetch_html_truncates_body_at_max_bytes():
    body = (b"<head>" + b"x" * 10_000 + b"</head>")
    handler = _make_handler(body)
    with _running_server(handler) as base:
        text = _fetch_html(f"{base}/page.html", max_bytes=100)
    # Truncated to 100 bytes (utf-8 of these ASCII chars is 1 byte each).
    assert len(text) == 100


def test_fetch_html_404_raises_http_error():
    handler = _make_handler(b"nope", status=404)
    with _running_server(handler) as base:
        with pytest.raises(urllib.error.HTTPError):
            _fetch_html(f"{base}/missing.html")


# ---------------------------------------------------------------------------
# fetch_url_preview — entry-point boundary
# ---------------------------------------------------------------------------


def test_fetch_url_preview_rejects_http_scheme(monkeypatch):
    """Non-HTTPS → UrlPreviewError, no network attempted."""
    called: list[str] = []

    def must_not_be_called(*args, **kwargs):
        called.append("yes")
        raise AssertionError("_fetch_html should not be called for non-HTTPS URLs")

    monkeypatch.setattr("news_agent.url_preview._fetch_html", must_not_be_called)

    with pytest.raises(UrlPreviewError):
        fetch_url_preview("http://example.com/")
    assert called == []


def test_fetch_url_preview_rejects_file_scheme():
    with pytest.raises(UrlPreviewError):
        fetch_url_preview("file:///etc/passwd")


def test_fetch_url_preview_happy_path_via_monkeypatched_fetcher(monkeypatch):
    """End-to-end at the boundary: HTTPS check passes, fetch + extract run."""
    canned_html = """
    <html><head>
      <meta property="og:title" content="Mocked">
      <meta property="og:description" content="Mock desc">
      <meta property="og:image" content="https://img/x.png">
    </head></html>
    """

    def fake_fetch(url, *, timeout, max_bytes, opener=None):
        assert url == "https://example.com/x"
        return canned_html

    monkeypatch.setattr("news_agent.url_preview._fetch_html", fake_fetch)

    out = fetch_url_preview("https://example.com/x")
    assert out.title == "Mocked"
    assert out.description == "Mock desc"
    assert out.image_url == "https://img/x.png"
    # No og:url + no canonical → echoes input URL.
    assert out.url == "https://example.com/x"


def test_fetch_url_preview_default_max_bytes_is_512k():
    """Sanity-check the cap matches the Rust server side."""
    assert MAX_BODY_BYTES == 512 * 1024
