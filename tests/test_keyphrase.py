"""Tests for keyphrase — blake3 + argon2id derivation."""

from __future__ import annotations

import pytest

from news_agent.keyphrase import (
    ARGON2_MEMORY_KIB,
    ARGON2_OUTPUT_BYTES,
    ARGON2_PARALLELISM,
    ARGON2_TIME_COST,
    derive_keyphrase,
)

# Cheap parameters for fast tests. Production cost is too heavy to run repeatedly.
FAST_KWARGS = dict(
    memory_kib=8 * 1024,   # 8 MiB
    time_cost=1,
    parallelism=1,
    output_bytes=32,
)


def test_production_parameters_are_locked_in():
    """The locked-in defaults shouldn't drift without an explicit decision."""
    assert ARGON2_MEMORY_KIB == 1024 * 1024  # 1 GiB
    assert ARGON2_TIME_COST == 4
    assert ARGON2_PARALLELISM == 1
    assert ARGON2_OUTPUT_BYTES == 64


def test_derivation_is_deterministic():
    a = derive_keyphrase("global-X", "local-Y", **FAST_KWARGS)
    b = derive_keyphrase("global-X", "local-Y", **FAST_KWARGS)
    assert a == b


def test_different_local_salts_produce_different_keyphrases():
    a = derive_keyphrase("global", "local-A", **FAST_KWARGS)
    b = derive_keyphrase("global", "local-B", **FAST_KWARGS)
    assert a != b


def test_different_global_salts_produce_different_keyphrases():
    a = derive_keyphrase("global-A", "local", **FAST_KWARGS)
    b = derive_keyphrase("global-B", "local", **FAST_KWARGS)
    assert a != b


def test_output_length_matches_request():
    out = derive_keyphrase("g", "l", output_bytes=32, memory_kib=8 * 1024, time_cost=1, parallelism=1)
    assert len(out) == 32 * 2  # hex is 2 chars per byte


def test_default_output_is_64_bytes_hex():
    """When the production output_bytes default is used, the result is 128 hex chars."""
    out = derive_keyphrase("g", "l", memory_kib=8 * 1024, time_cost=1, parallelism=1)
    assert len(out) == ARGON2_OUTPUT_BYTES * 2


def test_empty_salts_rejected():
    with pytest.raises(ValueError):
        derive_keyphrase("", "local", **FAST_KWARGS)
    with pytest.raises(ValueError):
        derive_keyphrase("global", "", **FAST_KWARGS)


def test_unicode_salts_round_trip_safely():
    # The function should handle non-ASCII characters without crashing.
    out_a = derive_keyphrase("café-grand-α", "ümlaut-locãl", **FAST_KWARGS)
    out_b = derive_keyphrase("café-grand-α", "ümlaut-locãl", **FAST_KWARGS)
    assert out_a == out_b
    assert len(out_a) == 32 * 2


def test_domain_salt_changes_output():
    base = derive_keyphrase("g", "l", **FAST_KWARGS)
    other = derive_keyphrase(
        "g", "l", domain_salt=b"news-agent-v999", **FAST_KWARGS
    )
    assert base != other
