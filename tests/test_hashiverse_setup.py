"""Tests for hashiverse_setup — first-run-derives, second-run-uses-cache.

Uses a fake HashiverseClient factory so the real client (with tokio runtime +
network transport) is never constructed. The test focuses on the cache-or-
derive decision and the client_id persistence path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from news_agent.config import IdentityConfig
from news_agent.hashiverse_setup import (
    CLIENT_ID_FILENAME,
    start_hashiverse_client_for_identity,
)


@dataclass
class _FakeClient:
    client_id: str


class _FakeClientFactory:
    """Records every call so tests can assert which constructor was used."""

    def __init__(self, client_id_to_return: str) -> None:
        self.client_id_to_return = client_id_to_return
        self.from_keyphrase_calls: list[dict] = []
        self.from_stored_key_calls: list[dict] = []

    def create_from_keyphrase(
        self,
        key_phrase: str,
        data_dir: str,
        passphrase: str = "",
        bootstrap_addresses: list[str] | None = None,
    ) -> _FakeClient:
        self.from_keyphrase_calls.append(
            dict(
                key_phrase=key_phrase,
                data_dir=data_dir,
                passphrase=passphrase,
                bootstrap_addresses=bootstrap_addresses,
            )
        )
        return _FakeClient(client_id=self.client_id_to_return)

    def create_from_stored_key(
        self,
        client_id_hex: str,
        data_dir: str,
        passphrase: str = "",
        bootstrap_addresses: list[str] | None = None,
    ) -> _FakeClient:
        self.from_stored_key_calls.append(
            dict(
                client_id_hex=client_id_hex,
                data_dir=data_dir,
                passphrase=passphrase,
                bootstrap_addresses=bootstrap_addresses,
            )
        )
        return _FakeClient(client_id=client_id_hex)


SALT = "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
CLIENT_ID = "ab" * 32


def _identity() -> IdentityConfig:
    return IdentityConfig(
        salt=SALT,
        nickname="Test",
        status="status",
        max_posts_per_day=1,
        sources=("https://example.com/rss",),
    )


def _fake_class(client_id: str) -> type:
    """Build a fake class object exposing the static-method interface."""
    factory = _FakeClientFactory(client_id)

    class FakeClass:
        @staticmethod
        def create_from_keyphrase(**kwargs):
            return factory.create_from_keyphrase(**kwargs)

        @staticmethod
        def create_from_stored_key(**kwargs):
            return factory.create_from_stored_key(**kwargs)

    FakeClass._factory = factory  # type: ignore[attr-defined]
    return FakeClass


def test_first_run_derives_keyphrase_and_persists_client_id(tmp_path):
    derive_calls: list[tuple[str, str]] = []

    def fake_derive(global_salt: str, local_salt: str) -> str:
        derive_calls.append((global_salt, local_salt))
        return "derived-fast-keyphrase"

    factory_class = _fake_class(CLIENT_ID)

    client = start_hashiverse_client_for_identity(
        identity=_identity(),
        identity_dir=tmp_path,
        global_salt="GLOBALX",
        client_factory=factory_class,
        derive_fn=fake_derive,
    )

    assert derive_calls == [("GLOBALX", SALT)]
    assert factory_class._factory.from_keyphrase_calls
    assert not factory_class._factory.from_stored_key_calls
    # client_id file is now on disk.
    cid_file = tmp_path / CLIENT_ID_FILENAME
    assert cid_file.exists()
    assert cid_file.read_text(encoding="utf-8") == CLIENT_ID
    # Returned client carries the client_id.
    assert client.client_id == CLIENT_ID


def test_second_run_uses_cached_client_id_and_skips_argon2(tmp_path):
    """If the cache exists, derive_fn is never called."""
    # Pre-populate the cached client_id.
    (tmp_path / CLIENT_ID_FILENAME).write_text(CLIENT_ID, encoding="utf-8")

    def fake_derive(global_salt: str, local_salt: str) -> str:
        raise AssertionError("derive_keyphrase should not be called when cache exists")

    factory_class = _fake_class(CLIENT_ID)

    client = start_hashiverse_client_for_identity(
        identity=_identity(),
        identity_dir=tmp_path,
        global_salt="GLOBALX",
        client_factory=factory_class,
        derive_fn=fake_derive,
    )

    assert factory_class._factory.from_stored_key_calls
    assert not factory_class._factory.from_keyphrase_calls
    call = factory_class._factory.from_stored_key_calls[0]
    assert call["client_id_hex"] == CLIENT_ID
    assert call["data_dir"] == str(tmp_path)
    assert call["passphrase"] == "GLOBALX"


def test_passphrase_passed_to_first_run_constructor(tmp_path):
    factory_class = _fake_class(CLIENT_ID)
    start_hashiverse_client_for_identity(
        identity=_identity(),
        identity_dir=tmp_path,
        global_salt="my-secret-salt-value",
        client_factory=factory_class,
        derive_fn=lambda *_: "k",
    )
    call = factory_class._factory.from_keyphrase_calls[0]
    assert call["passphrase"] == "my-secret-salt-value"
    assert call["data_dir"] == str(tmp_path)


def test_bootstrap_addresses_propagate(tmp_path):
    factory_class = _fake_class(CLIENT_ID)
    start_hashiverse_client_for_identity(
        identity=_identity(),
        identity_dir=tmp_path,
        global_salt="g",
        bootstrap_addresses=["bootstrap.example:443"],
        client_factory=factory_class,
        derive_fn=lambda *_: "k",
    )
    call = factory_class._factory.from_keyphrase_calls[0]
    assert call["bootstrap_addresses"] == ["bootstrap.example:443"]


def test_empty_cached_client_id_file_raises(tmp_path):
    """A blank file is suspicious — make the operator notice."""
    (tmp_path / CLIENT_ID_FILENAME).write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="empty"):
        start_hashiverse_client_for_identity(
            identity=_identity(),
            identity_dir=tmp_path,
            global_salt="g",
            client_factory=_fake_class(CLIENT_ID),
            derive_fn=lambda *_: "k",
        )


def test_atomic_write_leaves_no_tmp_files(tmp_path):
    factory_class = _fake_class(CLIENT_ID)
    start_hashiverse_client_for_identity(
        identity=_identity(),
        identity_dir=tmp_path,
        global_salt="g",
        client_factory=factory_class,
        derive_fn=lambda *_: "k",
    )
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert all(not name.endswith(".tmp") for name in siblings), siblings
