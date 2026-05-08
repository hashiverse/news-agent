"""Tests for hashiverse_setup — first-run-derives, second-run-uses-cache.

Uses a fake HashiverseClient factory so the real client (with tokio runtime +
network transport) is never constructed. The test focuses on the cache-or-
derive decision, the client_id persistence path, and the per-identity bio
sync (dry-run vs. production gating + on-disk last-bio cache).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from news_agent.config import IdentityConfig
from news_agent.hashiverse_setup import (
    CLIENT_ID_FILENAME,
    LAST_BIO_FILENAME,
    start_hashiverse_client_for_identity,
    update_bio_if_changed,
)


@dataclass
class _FakeClient:
    client_id: str
    set_bio_calls: list[tuple[str, str, str, str]] = field(default_factory=list)
    raise_on_set_bio: Exception | None = None

    def set_bio(self, nickname: str, status: str, selfie: str, avatar: str) -> None:
        if self.raise_on_set_bio is not None:
            raise self.raise_on_set_bio
        self.set_bio_calls.append((nickname, status, selfie, avatar))


class _FakeClientFactory:
    """Records every call so tests can assert which constructor was used.

    Returns a singleton _FakeClient so tests can read its set_bio_calls
    after start_hashiverse_client_for_identity returns (which fires the
    bio sync internally).
    """

    def __init__(self, client_id_to_return: str) -> None:
        self.client_id_to_return = client_id_to_return
        self.from_keyphrase_calls: list[dict] = []
        self.from_stored_key_calls: list[dict] = []
        self.client = _FakeClient(client_id=client_id_to_return)

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
        return self.client

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
        # Use the client_id from the stored-key call to mirror real behavior.
        self.client.client_id = client_id_hex
        return self.client


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
        dry_run=True,
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

    start_hashiverse_client_for_identity(
        identity=_identity(),
        identity_dir=tmp_path,
        global_salt="GLOBALX",
        client_factory=factory_class,
        derive_fn=fake_derive,
        dry_run=True,
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
        dry_run=True,
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
        dry_run=True,
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
            dry_run=True,
        )


def test_atomic_write_leaves_no_tmp_files(tmp_path):
    factory_class = _fake_class(CLIENT_ID)
    start_hashiverse_client_for_identity(
        identity=_identity(),
        identity_dir=tmp_path,
        global_salt="g",
        client_factory=factory_class,
        derive_fn=lambda *_: "k",
        dry_run=True,
    )
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert all(not name.endswith(".tmp") for name in siblings), siblings


# ---------------------------------------------------------------------------
# update_bio_if_changed — production sends + caches; dry-run logs only;
# unchanged bios are silent regardless of mode.


def _identity_with_selfie() -> IdentityConfig:
    return IdentityConfig(
        salt=SALT,
        nickname="Tesla News",
        status="auto-mirrored Tesla coverage",
        max_posts_per_day=1,
        sources=("https://example.com/rss",),
        selfie="data:image/png;base64,iVBORw0AAA",
    )


def _expected_bio_dict_from(identity: IdentityConfig) -> dict[str, str]:
    return {
        "nickname": identity.nickname,
        "status": identity.status,
        "selfie": identity.selfie or "",
        "avatar": "",
    }


def test_first_startup_in_production_sends_bio_and_writes_cache(tmp_path):
    identity = _identity_with_selfie()
    client = _FakeClient(client_id="x")
    sent = update_bio_if_changed(client, identity, tmp_path, dry_run=False)
    assert sent is True
    assert client.set_bio_calls == [
        (identity.nickname, identity.status, identity.selfie, ""),
    ]
    cache_path = tmp_path / LAST_BIO_FILENAME
    assert cache_path.exists()
    on_disk = json.loads(cache_path.read_text(encoding="utf-8"))
    assert on_disk == _expected_bio_dict_from(identity)


def test_unchanged_bio_skips_set_bio_call(tmp_path):
    """Cache matches identity → no set_bio, regardless of dry-run flag."""
    identity = _identity_with_selfie()
    (tmp_path / LAST_BIO_FILENAME).write_text(
        json.dumps(_expected_bio_dict_from(identity)), encoding="utf-8"
    )
    client = _FakeClient(client_id="x")
    assert update_bio_if_changed(client, identity, tmp_path, dry_run=False) is False
    assert update_bio_if_changed(client, identity, tmp_path, dry_run=True) is False
    assert client.set_bio_calls == []


def test_changed_status_triggers_send_and_cache_update(tmp_path):
    identity = _identity_with_selfie()
    old_bio = _expected_bio_dict_from(identity) | {"status": "stale status"}
    (tmp_path / LAST_BIO_FILENAME).write_text(json.dumps(old_bio), encoding="utf-8")
    client = _FakeClient(client_id="x")
    sent = update_bio_if_changed(client, identity, tmp_path, dry_run=False)
    assert sent is True
    assert client.set_bio_calls == [
        (identity.nickname, identity.status, identity.selfie, ""),
    ]
    on_disk = json.loads((tmp_path / LAST_BIO_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["status"] == identity.status


def test_corrupt_cache_treated_as_missing(tmp_path):
    """Garbage JSON shouldn't silently skip a real bio update."""
    (tmp_path / LAST_BIO_FILENAME).write_text("not json {{{", encoding="utf-8")
    client = _FakeClient(client_id="x")
    sent = update_bio_if_changed(client, _identity_with_selfie(), tmp_path, dry_run=False)
    assert sent is True
    assert len(client.set_bio_calls) == 1


def test_partial_cache_treated_as_missing(tmp_path):
    """Cache missing a required key is not trustworthy — re-send."""
    (tmp_path / LAST_BIO_FILENAME).write_text(
        json.dumps({"nickname": "x"}), encoding="utf-8"
    )
    client = _FakeClient(client_id="x")
    sent = update_bio_if_changed(client, _identity_with_selfie(), tmp_path, dry_run=False)
    assert sent is True
    assert len(client.set_bio_calls) == 1


def test_set_bio_failure_leaves_cache_unchanged(tmp_path):
    """If set_bio raises, the cache file must NOT be updated — next startup retries."""
    identity = _identity_with_selfie()
    client = _FakeClient(client_id="x", raise_on_set_bio=RuntimeError("network down"))
    with pytest.raises(RuntimeError, match="network down"):
        update_bio_if_changed(client, identity, tmp_path, dry_run=False)
    assert not (tmp_path / LAST_BIO_FILENAME).exists()


def test_dry_run_logs_would_send_and_does_not_call_set_bio(tmp_path, caplog):
    """Dry-run with no cache → log line, no network call, no cache write."""
    identity = _identity_with_selfie()
    client = _FakeClient(client_id="x")
    with caplog.at_level(logging.INFO, logger="news_agent.hashiverse_setup"):
        sent = update_bio_if_changed(client, identity, tmp_path, dry_run=True)
    assert sent is False
    assert client.set_bio_calls == []
    assert not (tmp_path / LAST_BIO_FILENAME).exists()
    matching = [
        r.getMessage() for r in caplog.records
        if "would send bio update" in r.getMessage()
    ]
    assert len(matching) == 1
    assert "[DRY-RUN]" in matching[0]


def test_dry_run_does_not_update_cache_even_when_bio_differs(tmp_path):
    """Dry-run must NOT update the cache — otherwise a subsequent production
    run would skip the (real) update because the cache looked current."""
    identity = _identity_with_selfie()
    old_bio = _expected_bio_dict_from(identity) | {"status": "stale status"}
    (tmp_path / LAST_BIO_FILENAME).write_text(json.dumps(old_bio), encoding="utf-8")
    client = _FakeClient(client_id="x")
    update_bio_if_changed(client, identity, tmp_path, dry_run=True)
    assert client.set_bio_calls == []
    on_disk = json.loads((tmp_path / LAST_BIO_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["status"] == "stale status"  # untouched


def test_dry_run_with_unchanged_bio_is_silent(tmp_path, caplog):
    """When the cache matches the current bio, dry-run produces NO log line —
    matches what production would do (nothing)."""
    identity = _identity_with_selfie()
    (tmp_path / LAST_BIO_FILENAME).write_text(
        json.dumps(_expected_bio_dict_from(identity)), encoding="utf-8"
    )
    client = _FakeClient(client_id="x")
    with caplog.at_level(logging.INFO, logger="news_agent.hashiverse_setup"):
        update_bio_if_changed(client, identity, tmp_path, dry_run=True)
    assert client.set_bio_calls == []
    assert not any(
        "would send bio update" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# start_hashiverse_client_for_identity bio-sync integration


def test_client_startup_invokes_bio_sync_in_production(tmp_path):
    identity = _identity_with_selfie()
    factory_class = _fake_class(CLIENT_ID)
    start_hashiverse_client_for_identity(
        identity=identity,
        identity_dir=tmp_path,
        global_salt="g",
        client_factory=factory_class,
        derive_fn=lambda *_: "k",
        dry_run=False,
    )
    assert factory_class._factory.client.set_bio_calls == [
        (identity.nickname, identity.status, identity.selfie, ""),
    ]
    assert (tmp_path / LAST_BIO_FILENAME).exists()


def test_client_startup_in_dry_run_does_not_send_bio(tmp_path):
    identity = _identity_with_selfie()
    factory_class = _fake_class(CLIENT_ID)
    client = start_hashiverse_client_for_identity(
        identity=identity,
        identity_dir=tmp_path,
        global_salt="g",
        client_factory=factory_class,
        derive_fn=lambda *_: "k",
        dry_run=True,
    )
    assert factory_class._factory.client.set_bio_calls == []
    assert not (tmp_path / LAST_BIO_FILENAME).exists()
    # Client still constructed and returned.
    assert client.client_id == CLIENT_ID


def test_client_startup_swallows_bio_failure(tmp_path, caplog):
    """A set_bio network error must not block client construction."""
    factory = _FakeClientFactory(CLIENT_ID)
    factory.client.raise_on_set_bio = RuntimeError("kaboom")

    class FakeClass:
        @staticmethod
        def create_from_keyphrase(**kwargs):
            return factory.create_from_keyphrase(**kwargs)

        @staticmethod
        def create_from_stored_key(**kwargs):
            return factory.create_from_stored_key(**kwargs)

    with caplog.at_level(logging.WARNING, logger="news_agent.hashiverse_setup"):
        client = start_hashiverse_client_for_identity(
            identity=_identity_with_selfie(),
            identity_dir=tmp_path,
            global_salt="g",
            client_factory=FakeClass,
            derive_fn=lambda *_: "k",
            dry_run=False,
        )
    # Client returned despite bio failure.
    assert client.client_id == CLIENT_ID
    # Warning logged.
    assert any(
        "set_bio failed" in r.getMessage() for r in caplog.records
    )
    # Cache file NOT written (set_bio raised before we could persist).
    assert not (tmp_path / LAST_BIO_FILENAME).exists()
