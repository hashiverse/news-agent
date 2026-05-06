"""Tests for config — YAML control-file loader and per-identity validation."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from news_agent.config import ControlFileError, IdentityConfig, load_control

FIXTURES = Path(__file__).parent / "fixtures"


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_load_minimal_yields_two_identities():
    config = load_control(FIXTURES / "control_minimal.yaml")
    assert len(config.identities) == 2
    names = [i.name for i in config.identities]
    assert names == ["bbc-mirror", "science-mirror"]


def test_defaults_applied_when_omitted():
    config = load_control(FIXTURES / "control_minimal.yaml")
    science = next(i for i in config.identities if i.name == "science-mirror")
    assert science.enabled is True
    assert science.exclude_selectors == ()
    assert science.exclude_urls == ()
    assert science.selfie is None


def test_short_salt_is_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        config = load_control(FIXTURES / "control_invalid_salt.yaml")
    names = [i.name for i in config.identities]
    assert names == ["valid-ident"]
    assert any("short-salt-ident" in record.message for record in caplog.records)
    assert any("salt is too short" in record.message for record in caplog.records)


def test_missing_required_field_skips_identity(tmp_path, caplog):
    yaml_text = """
identities:
  - name: missing-status
    salt: "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
    description: "no status field"
    nickname: "x"
    max_posts_per_day: 1
    include_selectors:
      - /topic/news/*
  - name: ok
    salt: "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
    description: "fine"
    nickname: "ok"
    status: "ok"
    max_posts_per_day: 1
    include_selectors:
      - /topic/news/*
"""
    path = _write(tmp_path / "c.yaml", yaml_text)
    with caplog.at_level(logging.WARNING):
        config = load_control(path)
    assert [i.name for i in config.identities] == ["ok"]
    assert any("missing-status" in record.message for record in caplog.records)


def test_empty_include_selectors_skips_identity(tmp_path):
    yaml_text = """
identities:
  - name: empty-selectors
    salt: "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
    description: "x"
    nickname: "x"
    status: "x"
    max_posts_per_day: 1
    include_selectors: []
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()


def test_duplicate_identity_name_raises(tmp_path):
    yaml_text = """
identities:
  - name: dupe
    salt: "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
    description: "x"
    nickname: "x"
    status: "x"
    max_posts_per_day: 1
    include_selectors: ["/topic/news/*"]
  - name: dupe
    salt: "c3a7e2f1b9d4a8e6c2f5d1b8e4a7c3f6d2b9e5a8c1f4d7b3e9a6c2f5d8b1e4c7"
    description: "x"
    nickname: "x"
    status: "x"
    max_posts_per_day: 1
    include_selectors: ["/topic/news/*"]
"""
    with pytest.raises(ControlFileError, match="duplicate identity name"):
        load_control(_write(tmp_path / "c.yaml", yaml_text))


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
    yaml_text = """
identities:
  - name: paused
    salt: "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
    description: "x"
    nickname: "x"
    status: "x"
    enabled: false
    max_posts_per_day: 1
    include_selectors: ["/topic/news/*"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities[0].enabled is False


def test_selfie_data_url_is_passed_through(tmp_path):
    selfie_url = "data:image/png;base64,iVBORw0KGgo="
    yaml_text = f"""
identities:
  - name: with-selfie
    salt: "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
    description: "x"
    nickname: "x"
    status: "x"
    selfie: "{selfie_url}"
    max_posts_per_day: 1
    include_selectors: ["/topic/news/*"]
"""
    config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities[0].selfie == selfie_url


def test_negative_max_posts_per_day_skipped(tmp_path, caplog):
    yaml_text = """
identities:
  - name: bad-cap
    salt: "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
    description: "x"
    nickname: "x"
    status: "x"
    max_posts_per_day: -3
    include_selectors: ["/topic/news/*"]
"""
    with caplog.at_level(logging.WARNING):
        config = load_control(_write(tmp_path / "c.yaml", yaml_text))
    assert config.identities == ()
    assert any("max_posts_per_day" in record.message for record in caplog.records)
