"""Tests for global_salt — env var handling, blake3 hashing, daemon-dir naming."""

from __future__ import annotations

import logging

import pytest

from news_agent.global_salt import (
    GLOBAL_SALT_ENV_VAR,
    MissingGlobalSaltError,
    daemon_home_root,
    load_global_salt,
)


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """A throwaway home directory."""
    return tmp_path


def test_missing_env_var_raises(monkeypatch, tmp_home):
    monkeypatch.delenv(GLOBAL_SALT_ENV_VAR, raising=False)
    with pytest.raises(MissingGlobalSaltError):
        load_global_salt(home=tmp_home)


def test_empty_env_var_raises(monkeypatch, tmp_home):
    monkeypatch.setenv(GLOBAL_SALT_ENV_VAR, "")
    with pytest.raises(MissingGlobalSaltError):
        load_global_salt(home=tmp_home)


def test_daemon_dir_is_deterministic(monkeypatch, tmp_home):
    monkeypatch.setenv(GLOBAL_SALT_ENV_VAR, "deterministic-salt-value")
    first = load_global_salt(home=tmp_home).daemon_dir
    second = load_global_salt(home=tmp_home).daemon_dir
    assert first == second


def test_different_salts_produce_different_dirs(monkeypatch, tmp_home):
    monkeypatch.setenv(GLOBAL_SALT_ENV_VAR, "salt-A")
    a = load_global_salt(home=tmp_home).daemon_dir
    monkeypatch.setenv(GLOBAL_SALT_ENV_VAR, "salt-B")
    b = load_global_salt(home=tmp_home).daemon_dir
    assert a != b


def test_daemon_dir_lives_under_news_agent_home(monkeypatch, tmp_home):
    monkeypatch.setenv(GLOBAL_SALT_ENV_VAR, "any-salt")
    salt = load_global_salt(home=tmp_home)
    assert salt.daemon_dir.parent == daemon_home_root(home=tmp_home)
    # Directory name is the blake3 hex digest — 64 chars of 0-9a-f.
    name = salt.daemon_dir.name
    assert len(name) == 64
    assert all(c in "0123456789abcdef" for c in name)


def test_short_salt_emits_warning(monkeypatch, tmp_home, caplog):
    monkeypatch.setenv(GLOBAL_SALT_ENV_VAR, "tooshort")  # 8 chars
    with caplog.at_level(logging.WARNING):
        salt = load_global_salt(home=tmp_home)
    assert salt.is_short
    assert any("disgustingly short" in record.message for record in caplog.records)


def test_long_salt_does_not_emit_warning(monkeypatch, tmp_home, caplog):
    long_salt = "x" * 64
    monkeypatch.setenv(GLOBAL_SALT_ENV_VAR, long_salt)
    with caplog.at_level(logging.WARNING):
        salt = load_global_salt(home=tmp_home)
    assert not salt.is_short
    assert not any("disgustingly short" in record.message for record in caplog.records)
