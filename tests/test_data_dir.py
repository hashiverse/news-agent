"""Tests for data_dir — daemon dir gating and per-identity tree creation."""

from __future__ import annotations

from pathlib import Path

import pytest

from news_agent.config import IdentityConfig
from news_agent.data_dir import (
    DaemonDirMissingError,
    ensure_cache_dir,
    ensure_daemon_dir,
    ensure_identity_dirs,
)
from news_agent.global_salt import GlobalSalt

LONG_SALT_A = "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
LONG_SALT_B = "c3a7e2f1b9d4a8e6c2f5d1b8e4a7c3f6d2b9e5a8c1f4d7b3e9a6c2f5d8b1e4c7"


def _make_salt(tmp_path: Path) -> GlobalSalt:
    return GlobalSalt(raw_value="x" * 64, daemon_dir=tmp_path / "daemon")


def _make_identity(name: str, salt: str) -> IdentityConfig:
    return IdentityConfig(
        name=name,
        salt=salt,
        description="d",
        nickname="n",
        status="s",
        max_posts_per_day=1,
        include_selectors=("/topic/news/*",),
    )


def test_missing_daemon_dir_without_create_new_raises(tmp_path):
    salt = _make_salt(tmp_path)
    with pytest.raises(DaemonDirMissingError):
        ensure_daemon_dir(salt, create_new=False)


def test_create_new_makes_daemon_dir(tmp_path):
    salt = _make_salt(tmp_path)
    path = ensure_daemon_dir(salt, create_new=True)
    assert path.is_dir()
    assert path == salt.daemon_dir


def test_existing_daemon_dir_returned_without_create_new(tmp_path):
    salt = _make_salt(tmp_path)
    salt.daemon_dir.mkdir(parents=True)
    path = ensure_daemon_dir(salt, create_new=False)
    assert path == salt.daemon_dir


def test_existing_daemon_dir_with_create_new_is_noop(tmp_path):
    salt = _make_salt(tmp_path)
    salt.daemon_dir.mkdir(parents=True)
    (salt.daemon_dir / "existing-file").write_text("preserved")
    ensure_daemon_dir(salt, create_new=True)
    assert (salt.daemon_dir / "existing-file").read_text() == "preserved"


def test_daemon_path_is_file_not_dir_raises(tmp_path):
    daemon_dir = tmp_path / "daemon"
    daemon_dir.write_text("oops")
    salt = GlobalSalt(raw_value="x" * 64, daemon_dir=daemon_dir)
    with pytest.raises(DaemonDirMissingError):
        ensure_daemon_dir(salt, create_new=False)


def test_ensure_identity_dirs_creates_per_identity_subdirs(tmp_path):
    salt = _make_salt(tmp_path)
    salt.daemon_dir.mkdir()
    identities = [
        _make_identity("a", LONG_SALT_A),
        _make_identity("b", LONG_SALT_B),
    ]
    result = ensure_identity_dirs(salt.daemon_dir, identities)
    assert [r.identity_name for r in result] == ["a", "b"]
    for ident_dir in result:
        assert ident_dir.path.is_dir()
        assert ident_dir.path.parent == salt.daemon_dir
    assert (salt.daemon_dir / LONG_SALT_A).is_dir()
    assert (salt.daemon_dir / LONG_SALT_B).is_dir()


def test_ensure_identity_dirs_is_idempotent(tmp_path):
    salt = _make_salt(tmp_path)
    salt.daemon_dir.mkdir()
    identities = [_make_identity("a", LONG_SALT_A)]
    ensure_identity_dirs(salt.daemon_dir, identities)
    # Drop a file inside; second call must not wipe it.
    (salt.daemon_dir / LONG_SALT_A / "existing-file").write_text("kept")
    ensure_identity_dirs(salt.daemon_dir, identities)
    assert (salt.daemon_dir / LONG_SALT_A / "existing-file").read_text() == "kept"


def test_identity_path_collision_with_file_raises(tmp_path):
    salt = _make_salt(tmp_path)
    salt.daemon_dir.mkdir()
    (salt.daemon_dir / LONG_SALT_A).write_text("not-a-directory")
    with pytest.raises(RuntimeError):
        ensure_identity_dirs(
            salt.daemon_dir, [_make_identity("a", LONG_SALT_A)]
        )


def test_ensure_cache_dir_creates_and_is_idempotent(tmp_path):
    salt = _make_salt(tmp_path)
    salt.daemon_dir.mkdir()
    cache = ensure_cache_dir(salt.daemon_dir)
    assert cache == salt.daemon_dir / "cache"
    assert cache.is_dir()
    # Drop a file inside; second call must not wipe it.
    (cache / "existing").write_text("kept")
    ensure_cache_dir(salt.daemon_dir)
    assert (cache / "existing").read_text() == "kept"
