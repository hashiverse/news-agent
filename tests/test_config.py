"""Tests for config — YAML control-file loader and per-identity validation."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from news_agent.config import ControlFileError, IdentityConfig, load_control

FIXTURES = Path(__file__).parent / "fixtures"

SALT_A = "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
SALT_B = "c3a7e2f1b9d4a8e6c2f5d1b8e4a7c3f6d2b9e5a8c1f4d7b3e9a6c2f5d8b1e4c7"


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_load_minimal_yields_two_identities():
    config = load_control(FIXTURES / "control_minimal.yaml")
    assert len(config.identities) == 2
    nicknames = [i.nickname for i in config.identities]
    assert nicknames == ["BBC Mirror", "Science Daily Mirror"]


def test_sources_are_preserved():
    config = load_control(FIXTURES / "control_minimal.yaml")
    bbc = next(i for i in config.identities if i.nickname == "BBC Mirror")
    assert bbc.sources == (
        "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
        "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    )


def test_defaults_applied_when_omitted():
    config = load_control(FIXTURES / "control_minimal.yaml")
    science = next(i for i in config.identities if i.nickname == "Science Daily Mirror")
    assert science.enabled is True
    assert science.selfie is None


def test_log_label_includes_nickname_and_salt_prefix():
    config = load_control(FIXTURES / "control_minimal.yaml")
    bbc = next(i for i in config.identities if i.nickname == "BBC Mirror")
    label = bbc.log_label
    assert "BBC Mirror" in label
    assert SALT_A[:8] in label


def test_short_salt_is_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        config = load_control(FIXTURES / "control_invalid_salt.yaml")
    assert len(config.identities) == 1
    assert config.identities[0].nickname == "Valid"
    # Friendly-cranky log line uses the nickname of the rejected identity.
    assert any("Short Salt" in record.message for record in caplog.records)
    assert any("salt is too short" in record.message for record in caplog.records)


def test_missing_required_field_skips_identity(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "missing status"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
  - salt: "{SALT_B}"
    nickname: "ok"
    status: "ok"
    max_posts_per_day: 1
    sources: ["https://example.com/b"]
"""
    path = _write(tmp_path / "c.yaml", yaml_text)
    with caplog.at_level(logging.WARNING):
        config = load_control(path)
    nicknames = [i.nickname for i in config.identities]
    assert nicknames == ["ok"]
    assert any(
        "missing or empty 'status' field" in record.message for record in caplog.records
    )


def test_missing_sources_skips_identity(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "no sources"
    status: "x"
    max_posts_per_day: 1
"""
    with caplog.at_level(logging.WARNING):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()
    assert any("'sources' is required" in record.message for record in caplog.records)


def test_empty_sources_skips_identity(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "empty sources"
    status: "x"
    max_posts_per_day: 1
    sources: []
"""
    with caplog.at_level(logging.WARNING):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()
    assert any("'sources' is required" in record.message for record in caplog.records)


def test_non_string_source_skips_identity(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "bad-sources"
    status: "x"
    max_posts_per_day: 1
    sources:
      - 42
"""
    with caplog.at_level(logging.WARNING):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()
    assert any("contains non-string entry" in record.message for record in caplog.records)


def test_duplicate_salt_raises(tmp_path):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "first"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
  - salt: "{SALT_A}"
    nickname: "second-with-same-salt"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/b"]
"""
    with pytest.raises(ControlFileError, match="duplicate salt"):
        load_control(_write(tmp_path / "c.yaml", yaml_text))


def test_duplicate_nickname_is_allowed(tmp_path):
    """Two identities can share a nickname; the salt is what disambiguates."""
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "Mirror"
    status: "first"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
  - salt: "{SALT_B}"
    nickname: "Mirror"
    status: "second"
    max_posts_per_day: 1
    sources: ["https://example.com/b"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert len(config.identities) == 2


def test_malformed_yaml_raises(tmp_path):
    path = _write(tmp_path / "bad.yaml", "identities: [unbalanced")
    with pytest.raises(ControlFileError):
        load_control(path)


def test_top_level_not_mapping_raises(tmp_path):
    path = _write(tmp_path / "list.yaml", "- 1\n- 2\n")
    with pytest.raises(ControlFileError):
        load_control(path)


def test_empty_yaml_yields_empty_config(tmp_path):
    path = _write(tmp_path / "empty.yaml", "")
    config = load_control(path)
    assert config.identities == ()


def test_enabled_false_is_preserved(tmp_path):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "paused"
    status: "x"
    enabled: false
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities[0].enabled is False


def test_selfie_data_url_is_passed_through(tmp_path):
    selfie_url = "data:image/png;base64,iVBORw0KGgo="
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "with selfie"
    status: "x"
    selfie: "{selfie_url}"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities[0].selfie == selfie_url


def test_hashtags_omitted_defaults_to_empty(tmp_path):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "no-tags"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities[0].hashtags == ()


def test_hashtags_round_trip(tmp_path):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "tagged"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
    hashtags: ["news", "tech"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities[0].hashtags == ("news", "tech")


def test_hashtags_leading_hash_is_stripped(tmp_path):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "tagged"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
    hashtags: ["#news", "##tech", "ok"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities[0].hashtags == ("news", "tech", "ok")


def test_hashtags_only_hash_chars_skips_identity(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "bad-tags"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
    hashtags: ["##"]
"""
    with caplog.at_level(logging.WARNING):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()
    assert any(
        "empty after stripping '#'" in record.message for record in caplog.records
    )


def test_hashtags_non_list_skips_identity(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "bad-tags"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
    hashtags: "news"
"""
    with caplog.at_level(logging.WARNING):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()
    assert any(
        "hashtags must be a list of strings" in record.message for record in caplog.records
    )


def test_hashtags_non_string_entry_skips_identity(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "bad-tags"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
    hashtags:
      - 42
"""
    with caplog.at_level(logging.WARNING):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()
    assert any(
        "hashtags contains non-string entry" in record.message for record in caplog.records
    )


def test_negative_max_posts_per_day_skipped(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "bad cap"
    status: "x"
    max_posts_per_day: -3
    sources: ["https://example.com/a"]
"""
    with caplog.at_level(logging.WARNING):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()
    assert any("max_posts_per_day" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Cross-identity duplicate-source warning
# ---------------------------------------------------------------------------


def test_duplicate_source_across_identities_warns(tmp_path, caplog):
    shared = "https://example.com/shared.xml"
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "alpha"
    status: "x"
    max_posts_per_day: 1
    sources:
      - {shared}
      - https://example.com/alpha-only.xml
  - salt: "{SALT_B}"
    nickname: "beta"
    status: "x"
    max_posts_per_day: 1
    sources:
      - {shared}
"""
    with caplog.at_level(logging.WARNING):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert len(config.identities) == 2
    matching = [
        record.message for record in caplog.records
        if shared in record.message and "appears in" in record.message
    ]
    assert matching, f"expected duplicate-source warning, got: {[r.message for r in caplog.records]}"
    assert any("alpha" in m and "beta" in m for m in matching)


def test_unique_sources_emit_no_duplicate_warning(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "alpha"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
  - salt: "{SALT_B}"
    nickname: "beta"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/b"]
"""
    with caplog.at_level(logging.WARNING):
        load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert not any("appears in" in r.message for r in caplog.records)


def test_within_identity_repeated_source_does_not_trigger_cross_warning(tmp_path, caplog):
    """One identity listing the same source twice is not 'cross-identity' duplication."""
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "alpha"
    status: "x"
    max_posts_per_day: 1
    sources:
      - https://example.com/a
      - https://example.com/a
"""
    with caplog.at_level(logging.WARNING):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    # Identity is loaded with both copies preserved (we don't dedupe within an identity).
    assert config.identities[0].sources.count("https://example.com/a") == 2
    # No cross-identity duplicate warning fires (only one identity holds it).
    cross_warns = [r.message for r in caplog.records if "appears in" in r.message]
    assert cross_warns == []


# ---------------------------------------------------------------------------
# keywords_required / keywords_optional


def test_keywords_omitted_defaults_to_empty(tmp_path):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "no-keywords"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities[0].keywords_required == ()
    assert config.identities[0].keywords_optional == ()


def test_keywords_required_lowercased_at_load_time(tmp_path):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "rusty"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
    keywords_required: ["Rust", "ASYNC"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities[0].keywords_required == ("rust", "async")


def test_keywords_optional_lowercased_at_load_time(tmp_path):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "rusty"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
    keywords_optional: ["Rust", "WASM", "Tokio"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities[0].keywords_optional == ("rust", "wasm", "tokio")


def test_keywords_both_round_trip_independently(tmp_path):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "rusty"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
    keywords_required: ["rust"]
    keywords_optional: ["async", "threading"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    identity = config.identities[0]
    assert identity.keywords_required == ("rust",)
    assert identity.keywords_optional == ("async", "threading")


def test_keywords_required_non_list_skips_identity(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "bad"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
    keywords_required: "rust"
"""
    with caplog.at_level(logging.INFO):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()
    assert any(
        "keywords_required must be a list of strings" in record.message
        for record in caplog.records
    )


def test_keywords_optional_empty_string_entry_skips_identity(tmp_path, caplog):
    yaml_text = f"""
identities:
  - salt: "{SALT_A}"
    nickname: "bad"
    status: "x"
    max_posts_per_day: 1
    sources: ["https://example.com/a"]
    keywords_optional: ["rust", ""]
"""
    with caplog.at_level(logging.INFO):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()
    assert any(
        "keywords_optional contains an empty string" in record.message
        for record in caplog.records
    )
