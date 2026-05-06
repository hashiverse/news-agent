"""Tests for logging_helpers — random suggestions and friendly-cranky lines."""

from __future__ import annotations

import string

import pytest

from news_agent.logging_helpers import (
    MINIMUM_SALT_LENGTH,
    random_salt_suggestion,
    short_global_salt_warning,
    short_identity_salt_warning,
)


def test_random_salt_suggestion_default_length_is_at_least_minimum():
    suggestion = random_salt_suggestion()
    assert len(suggestion) >= MINIMUM_SALT_LENGTH


def test_random_salt_suggestion_respects_explicit_length():
    for length in (32, 48, 64, 100):
        assert len(random_salt_suggestion(length=length)) == length


def test_random_salt_suggestion_uses_path_safe_alphabet():
    allowed = set(string.ascii_letters + string.digits + "-_")
    for _ in range(50):
        suggestion = random_salt_suggestion()
        assert set(suggestion).issubset(allowed), suggestion


def test_random_salt_suggestion_varies_per_call():
    seen = {random_salt_suggestion() for _ in range(20)}
    assert len(seen) == 20, "expected unique suggestions across calls"


def test_random_salt_suggestion_rejects_non_positive_length():
    with pytest.raises(ValueError):
        random_salt_suggestion(length=0)
    with pytest.raises(ValueError):
        random_salt_suggestion(length=-3)


def test_short_global_salt_warning_mentions_actual_length():
    line = short_global_salt_warning(actual_length=8)
    assert "NEWS_AGENT_GLOBAL_SALT" in line
    assert "8 chars" in line
    assert str(MINIMUM_SALT_LENGTH) in line


def test_short_global_salt_warning_includes_a_suggestion():
    line = short_global_salt_warning(actual_length=4)
    # The line ends with the suggestion; the suggestion is path-safe and at least MINIMUM long.
    assert "try something random like " in line
    suggestion = line.split("try something random like ")[-1].strip()
    assert len(suggestion) >= MINIMUM_SALT_LENGTH


def test_short_identity_salt_warning_includes_identity_name_and_suggestion():
    line = short_identity_salt_warning("bbc-mirror", actual_length=10)
    assert "bbc-mirror" in line
    assert "10 chars" in line
    assert "you can use " in line
    suggestion = line.split("you can use ")[-1].strip()
    assert len(suggestion) >= MINIMUM_SALT_LENGTH


def test_warning_lines_vary_between_calls():
    lines = {short_global_salt_warning(actual_length=4) for _ in range(10)}
    assert len(lines) > 1, "expected fresh suggestions per call"
