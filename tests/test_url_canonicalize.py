"""Tests for url_canonicalize.canonicalize()."""

from __future__ import annotations

import pytest

from news_agent.url_canonicalize import canonicalize


@pytest.mark.parametrize(
    "input_url, expected",
    [
        # Basic identity case
        ("https://example.com/article", "https://example.com/article"),
        # Lowercase host
        ("https://EXAMPLE.com/article", "https://example.com/article"),
        # Lowercase scheme
        ("HTTPS://example.com/article", "https://example.com/article"),
        # Drop fragment
        ("https://example.com/article#section", "https://example.com/article"),
        ("https://example.com/article?x=1#section", "https://example.com/article?x=1"),
        # Drop trailing slash
        ("https://example.com/article/", "https://example.com/article"),
        # Preserve root slash
        ("https://example.com/", "https://example.com/"),
    ],
)
def test_basic_canonicalization(input_url: str, expected: str) -> None:
    assert canonicalize(input_url) == expected


@pytest.mark.parametrize(
    "param",
    ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"],
)
def test_strips_utm_parameters(param: str) -> None:
    url = f"https://example.com/article?{param}=foo"
    assert canonicalize(url) == "https://example.com/article"


def test_strips_known_tracking_params():
    cases = {
        "https://example.com/x?fbclid=abc": "https://example.com/x",
        "https://example.com/x?gclid=abc": "https://example.com/x",
        "https://example.com/x?mc_cid=abc": "https://example.com/x",
        "https://example.com/x?mc_eid=abc": "https://example.com/x",
        "https://example.com/x?_ga=abc": "https://example.com/x",
    }
    for inp, expected in cases.items():
        assert canonicalize(inp) == expected, inp


def test_keeps_non_tracking_params():
    url = "https://example.com/article?id=42&page=3"
    # Sorted on the way out — id, page is already sorted.
    assert canonicalize(url) == "https://example.com/article?id=42&page=3"


def test_query_param_order_is_normalised():
    a = canonicalize("https://example.com/article?b=2&a=1")
    b = canonicalize("https://example.com/article?a=1&b=2")
    assert a == b
    # Sorted alphabetically by key.
    assert a == "https://example.com/article?a=1&b=2"


def test_strips_only_tracking_params_among_others():
    url = "https://example.com/article?id=42&utm_source=email&page=3&fbclid=xxx"
    # Tracking params dropped, real ones kept and sorted.
    assert canonicalize(url) == "https://example.com/article?id=42&page=3"


def test_blank_value_query_params_preserved():
    """`?empty=` (blank value) is a real param, not noise — preserve it."""
    url = "https://example.com/article?empty=&id=42"
    assert canonicalize(url) == "https://example.com/article?empty=&id=42"


def test_idempotent():
    url = "HTTPS://Example.COM/Path/?utm_source=x&id=1#frag"
    once = canonicalize(url)
    twice = canonicalize(once)
    assert once == twice
    assert once == "https://example.com/Path?id=1"


def test_path_case_preserved():
    """Paths are case-sensitive in HTTP; only the host is lowercased."""
    url = "https://example.com/Articles/My-Title"
    assert canonicalize(url) == "https://example.com/Articles/My-Title"


def test_no_netloc_returns_input_unchanged():
    """Bare paths or odd schemes without a host pass through."""
    assert canonicalize("/relative/path") == "/relative/path"
    assert canonicalize("data:image/png;base64,abc") == "data:image/png;base64,abc"


def test_port_in_netloc_preserved():
    assert (
        canonicalize("https://EXAMPLE.com:8080/path")
        == "https://example.com:8080/path"
    )


def test_two_urls_differing_only_in_tracking_dedupe_to_same_string():
    a = canonicalize("https://example.com/article?utm_source=newsletter")
    b = canonicalize("https://example.com/article?utm_source=facebook")
    c = canonicalize("https://example.com/article")
    assert a == b == c
