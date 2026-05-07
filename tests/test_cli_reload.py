"""Tests for the full-rebuild reload behaviour in cli.py.

Verifies that on every successful reload of the control file, the in-memory
``clients`` dict is fully rebuilt from disk:

- Identities added to the control file appear in ``clients`` after reload.
- Identities removed (or flipped to ``enabled: false``) disappear.
- Identities flipped back from ``enabled: false`` to ``true`` reappear.
- Even when the identity set is unchanged, the start path is invoked again
  (full rebuild — no diff bookkeeping).

The tests substitute ``_start_clients_for_identities`` via monkeypatch so
real hashiverse clients (with tokio runtimes) are never constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from news_agent import cli
from news_agent.config import load_control
from news_agent.data_dir import IdentityDir
from news_agent.runtime_state import RuntimeSnapshot, RuntimeState

SALT_A = "8f4c2a1e9d7b6f3e5a8c2d1b4e7f9a3c6d8b1e4a7c2f5d9b8e1a4c7f2d5b8e1a"
SALT_B = "c3a7e2f1b9d4a8e6c2f5d1b8e4a7c3f6d2b9e5a8c1f4d7b3e9a6c2f5d8b1e4c7"
SALT_C = "1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff"


@dataclass
class _FakeClient:
    salt: str

    @property
    def client_id(self) -> str:
        return f"client-id-for-{self.salt[:8]}"


@pytest.fixture
def fake_start_clients(monkeypatch):
    """Replace _start_clients_for_identities with a deterministic fake.

    The fake records every invocation and returns a dict of one fake client
    per identity, keyed by salt.
    """
    invocations: list[list[str]] = []

    def fake(identities, identity_dirs, global_salt):
        invocations.append([i.salt for i in identities if i.enabled])
        return {i.salt: _FakeClient(salt=i.salt) for i in identities if i.enabled}

    monkeypatch.setattr(cli, "_start_clients_for_identities", fake)
    return invocations


@pytest.fixture
def daemon_dir(tmp_path):
    d = tmp_path / "daemon"
    d.mkdir()
    return d


@pytest.fixture
def control_path(tmp_path):
    return tmp_path / "control.yaml"


def _write_control(path: Path, identities_yaml: str) -> None:
    path.write_text(f"identities:\n{identities_yaml}", encoding="utf-8")


def _identity_block(salt: str, nickname: str, enabled: bool = True) -> str:
    enabled_str = "true" if enabled else "false"
    return f"""
  - salt: "{salt}"
    nickname: "{nickname}"
    status: "x"
    enabled: {enabled_str}
    max_posts_per_day: 1
    sources: ["https://example.com/{nickname}"]
"""


def _initial_setup(
    control_path: Path, daemon_dir: Path, identities_yaml: str
) -> tuple[dict[str, object], RuntimeState]:
    _write_control(control_path, identities_yaml)
    control = load_control(control_path)
    # Manually create per-identity dirs that ensure_identity_dirs would create.
    from news_agent.data_dir import ensure_identity_dirs
    ensure_identity_dirs(daemon_dir, control.identities)
    clients: dict[str, object] = {
        i.salt: _FakeClient(salt=i.salt) for i in control.identities if i.enabled
    }
    state = RuntimeState(RuntimeSnapshot(control=control))
    return clients, state


# ---------------------------------------------------------------------------


def test_reload_with_added_identity_brings_up_new_client(
    fake_start_clients, daemon_dir, control_path
):
    clients, state = _initial_setup(
        control_path, daemon_dir, _identity_block(SALT_A, "alpha")
    )
    assert set(clients.keys()) == {SALT_A}

    # Edit the control file to add a new identity.
    _write_control(
        control_path,
        _identity_block(SALT_A, "alpha") + _identity_block(SALT_B, "beta"),
    )

    cli._reload_state(
        clients=clients,
        state=state,
        control_path=control_path,
        daemon_dir=daemon_dir,
        global_salt="g",
    )

    assert set(clients.keys()) == {SALT_A, SALT_B}
    # The fake start was invoked with the full new identity set.
    assert fake_start_clients == [[SALT_A, SALT_B]]


def test_reload_with_removed_identity_drops_client(
    fake_start_clients, daemon_dir, control_path
):
    clients, state = _initial_setup(
        control_path,
        daemon_dir,
        _identity_block(SALT_A, "alpha") + _identity_block(SALT_B, "beta"),
    )
    assert set(clients.keys()) == {SALT_A, SALT_B}

    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    cli._reload_state(
        clients=clients,
        state=state,
        control_path=control_path,
        daemon_dir=daemon_dir,
        global_salt="g",
    )

    assert set(clients.keys()) == {SALT_A}


def test_reload_with_disabled_identity_drops_client(
    fake_start_clients, daemon_dir, control_path
):
    clients, state = _initial_setup(
        control_path,
        daemon_dir,
        _identity_block(SALT_A, "alpha") + _identity_block(SALT_B, "beta"),
    )
    assert set(clients.keys()) == {SALT_A, SALT_B}

    _write_control(
        control_path,
        _identity_block(SALT_A, "alpha")
        + _identity_block(SALT_B, "beta", enabled=False),
    )

    cli._reload_state(
        clients=clients,
        state=state,
        control_path=control_path,
        daemon_dir=daemon_dir,
        global_salt="g",
    )

    assert set(clients.keys()) == {SALT_A}


def test_reload_re_enables_a_disabled_identity(
    fake_start_clients, daemon_dir, control_path
):
    clients, state = _initial_setup(
        control_path,
        daemon_dir,
        _identity_block(SALT_A, "alpha")
        + _identity_block(SALT_B, "beta", enabled=False),
    )
    assert set(clients.keys()) == {SALT_A}

    _write_control(
        control_path,
        _identity_block(SALT_A, "alpha") + _identity_block(SALT_B, "beta"),
    )

    cli._reload_state(
        clients=clients,
        state=state,
        control_path=control_path,
        daemon_dir=daemon_dir,
        global_salt="g",
    )

    assert set(clients.keys()) == {SALT_A, SALT_B}


def test_reload_with_no_identity_changes_still_rebuilds(
    fake_start_clients, daemon_dir, control_path
):
    """No-op-looking reloads still tear down and rebuild — that's the point."""
    clients, state = _initial_setup(
        control_path,
        daemon_dir,
        _identity_block(SALT_A, "alpha") + _identity_block(SALT_B, "beta"),
    )
    original_a = clients[SALT_A]
    original_b = clients[SALT_B]

    # Touch the file without changing identity set.
    _write_control(
        control_path,
        _identity_block(SALT_A, "alpha") + _identity_block(SALT_B, "beta"),
    )

    cli._reload_state(
        clients=clients,
        state=state,
        control_path=control_path,
        daemon_dir=daemon_dir,
        global_salt="g",
    )

    # Same salts present, but the dict's contents are fresh objects.
    assert set(clients.keys()) == {SALT_A, SALT_B}
    assert clients[SALT_A] is not original_a
    assert clients[SALT_B] is not original_b
    # And the start-clients function got invoked (i.e. we did rebuild).
    assert fake_start_clients == [[SALT_A, SALT_B]]


def test_reload_invalid_yaml_keeps_previous_state(
    fake_start_clients, daemon_dir, control_path, caplog
):
    clients, state = _initial_setup(
        control_path, daemon_dir, _identity_block(SALT_A, "alpha")
    )
    initial_clients = dict(clients)

    # Break the YAML.
    control_path.write_text("identities: [unbalanced", encoding="utf-8")

    cli._reload_state(
        clients=clients,
        state=state,
        control_path=control_path,
        daemon_dir=daemon_dir,
        global_salt="g",
    )

    # Previous state preserved.
    assert dict(clients) == initial_clients
    # No call to fake start was made.
    assert fake_start_clients == []


def test_reload_swaps_runtime_snapshot(
    fake_start_clients, daemon_dir, control_path
):
    clients, state = _initial_setup(
        control_path, daemon_dir, _identity_block(SALT_A, "alpha")
    )
    snapshot_before = state.snapshot()

    _write_control(
        control_path,
        _identity_block(SALT_A, "alpha") + _identity_block(SALT_C, "charlie"),
    )

    cli._reload_state(
        clients=clients,
        state=state,
        control_path=control_path,
        daemon_dir=daemon_dir,
        global_salt="g",
    )

    snapshot_after = state.snapshot()
    assert snapshot_after is not snapshot_before
    salts_after = {i.salt for i in snapshot_after.control.identities}
    assert salts_after == {SALT_A, SALT_C}
