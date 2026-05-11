"""Unit tests for the one-shot `test-hashiverse` smoke runner.

The runner takes ``client_factory`` and ``preview_fn`` parameters so these
tests can substitute fakes without touching the real Rust client or
making any network calls.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from typing import Any

import pytest

from news_agent.hashiverse_smoke import (
    TEST_POST_URL,
    run_hashiverse_smoke_test,
)
from news_agent.posting import format_post_html
from news_agent.rss_parser import Article
from news_agent.url_preview import UrlPreviewData


# The exact synthetic Article the smoke runner constructs internally; tests
# use it to compute the expected production-formatter output and assert
# byte-equality against what the smoke runner actually submits.
_EXPECTED_SYNTHETIC_ARTICLE = Article(
    title="",
    canonical_url=TEST_POST_URL,
    raw_url=TEST_POST_URL,
    item_guid=None,
    summary="",
    published_at_unix=None,
    source_url="",
)


@dataclass
class _ConstructorCall:
    key_phrase: str
    data_dir: str
    passphrase: str
    bootstrap_addresses: Any


class _FakeClient:
    def __init__(self, client_id: str = "fake-client-id-1234") -> None:
        self.client_id = client_id
        self.posted_bodies: list[str] = []

    def submit_post(self, html_body: str) -> None:
        self.posted_bodies.append(html_body)


class _RecordingFactory:
    """Records every constructor call and returns a fresh fake client each time."""

    def __init__(self) -> None:
        self.calls: list[_ConstructorCall] = []
        self.clients: list[_FakeClient] = []

    def __call__(
        self,
        *,
        key_phrase: str,
        data_dir: str,
        passphrase: str,
        bootstrap_addresses: Any,
    ) -> _FakeClient:
        self.calls.append(
            _ConstructorCall(
                key_phrase=key_phrase,
                data_dir=data_dir,
                passphrase=passphrase,
                bootstrap_addresses=bootstrap_addresses,
            )
        )
        client = _FakeClient(client_id=f"fake-{len(self.clients)}")
        self.clients.append(client)
        return client


def _preview_returning(preview: UrlPreviewData):
    seen: list[str] = []

    def _fn(url: str) -> UrlPreviewData:
        seen.append(url)
        return preview

    _fn.urls_seen = seen  # type: ignore[attr-defined]
    return _fn


# ---------------------------------------------------------------------------


def test_smoke_test_submits_post_built_by_production_formatter():
    """The submitted body is byte-identical to what `posting.format_post_html`
    would produce for the same synthetic Article + preview + ('test',) hashtags.
    Pins the 'smoke uses the exact production format' guarantee."""
    factory = _RecordingFactory()
    preview = UrlPreviewData(
        url=TEST_POST_URL,
        title="OG Title",
        description="OG Desc",
        image_url="https://img.example/x.png",
    )
    preview_fn = _preview_returning(preview)

    returned_url = run_hashiverse_smoke_test(
        client_factory=factory,
        preview_fn=preview_fn,
    )

    # One constructor call with the expected shape.
    assert len(factory.calls) == 1
    call = factory.calls[0]
    assert call.bootstrap_addresses is None  # → production DNSSEC bootstrap
    assert len(call.key_phrase) == 64 and all(c in "0123456789abcdef" for c in call.key_phrase)
    assert call.passphrase  # non-empty random salt
    assert call.data_dir  # non-empty path

    # preview_fn was invoked exactly once with the fixed test URL.
    assert preview_fn.urls_seen == [TEST_POST_URL]  # type: ignore[attr-defined]

    # Exactly one submit_post on the constructed client.
    client = factory.clients[0]
    assert len(client.posted_bodies) == 1

    # Byte-equality against the production formatter — strongest possible pin.
    expected_body = format_post_html(
        _EXPECTED_SYNTHETIC_ARTICLE, preview, hashtags=("test",)
    )
    assert client.posted_bodies[0] == expected_body

    assert returned_url == TEST_POST_URL


def test_smoke_test_falls_back_to_url_when_preview_returns_empty():
    """When the OG fetch returns nothing, the smoke runner's body still matches
    `format_post_html` byte-for-byte (which falls back to raw_url for title/url)."""
    factory = _RecordingFactory()
    blank_preview = UrlPreviewData()  # all blanks
    preview_fn = _preview_returning(blank_preview)

    returned_url = run_hashiverse_smoke_test(
        client_factory=factory,
        preview_fn=preview_fn,
    )

    expected_body = format_post_html(
        _EXPECTED_SYNTHETIC_ARTICLE, blank_preview, hashtags=("test",)
    )
    assert factory.clients[0].posted_bodies[0] == expected_body
    # Returned URL falls back to TEST_POST_URL when preview.url is blank.
    assert returned_url == TEST_POST_URL


def test_smoke_test_data_dir_is_under_temp():
    factory = _RecordingFactory()
    preview_fn = _preview_returning(UrlPreviewData(url=TEST_POST_URL))

    run_hashiverse_smoke_test(
        client_factory=factory,
        preview_fn=preview_fn,
    )

    data_dir = factory.calls[0].data_dir
    # Lives under the OS tempdir with the documented prefix.
    assert data_dir.startswith(tempfile.gettempdir())
    # The basename starts with the prefix we documented.
    import os as _os
    assert _os.path.basename(data_dir).startswith("news-agent-test-hashiverse-")


def test_smoke_test_each_invocation_uses_fresh_salts():
    """Two back-to-back invocations must derive *different* keyphrases — pins
    the 'random ephemeral identity per run' property so a future regression
    that accidentally fixes the salt is caught."""
    factory = _RecordingFactory()
    preview_fn = _preview_returning(UrlPreviewData(url=TEST_POST_URL))

    run_hashiverse_smoke_test(client_factory=factory, preview_fn=preview_fn)
    run_hashiverse_smoke_test(client_factory=factory, preview_fn=preview_fn)

    assert len(factory.calls) == 2
    a, b = factory.calls
    assert a.key_phrase != b.key_phrase, "expected fresh random keyphrase per invocation"
    assert a.passphrase != b.passphrase, "expected fresh random global salt per invocation"
