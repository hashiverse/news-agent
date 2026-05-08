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

Also covers smaller surfaces that live in cli.py: dry-run / production
flag wiring, `--verbose-hashiverse` / `--verbose-filtering` plumbing, and
the `_ColorFormatter` ANSI-color formatter for log output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from news_agent import cli
from news_agent.config import load_control
from news_agent.data_dir import IdentityDir
from news_agent.keyphrase import derive_keyphrase, derive_keyphrase_cheap
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

    def fake(identities, identity_dirs, global_salt, *, derive_fn):
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
        derive_fn=derive_keyphrase,
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
        derive_fn=derive_keyphrase,
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
        derive_fn=derive_keyphrase,
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
        derive_fn=derive_keyphrase,
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
        derive_fn=derive_keyphrase,
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
        derive_fn=derive_keyphrase,
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
        derive_fn=derive_keyphrase,
    )

    snapshot_after = state.snapshot()
    assert snapshot_after is not snapshot_before
    salts_after = {i.salt for i in snapshot_after.control.identities}
    assert salts_after == {SALT_A, SALT_C}


def test_reload_state_forwards_derive_fn(monkeypatch, daemon_dir, control_path):
    """_reload_state passes the derive_fn arg straight through to _start_clients_for_identities."""
    captured: list = []

    def fake(identities, identity_dirs, global_salt, *, derive_fn):
        captured.append(derive_fn)
        return {i.salt: _FakeClient(salt=i.salt) for i in identities if i.enabled}

    monkeypatch.setattr(cli, "_start_clients_for_identities", fake)

    _write_control(control_path, _identity_block(SALT_A, "alpha"))
    control = load_control(control_path)
    from news_agent.data_dir import ensure_identity_dirs
    ensure_identity_dirs(daemon_dir, control.identities)

    clients: dict[str, object] = {SALT_A: _FakeClient(salt=SALT_A)}
    state = RuntimeState(RuntimeSnapshot(control=control))

    def sentinel(_g: str, _l: str) -> str:
        return "sentinel"

    cli._reload_state(
        clients=clients,
        state=state,
        control_path=control_path,
        daemon_dir=daemon_dir,
        global_salt="g",
        derive_fn=sentinel,
    )

    assert captured == [sentinel]


# ---------------------------------------------------------------------------
# cli.run --test wiring (cheap vs. production argon2)


class _NoOpWatcher:
    def __init__(self, *_a, **_kw) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _stub_cli_run_side_effects(
    monkeypatch,
    *,
    captured_derive_fns: list,
    captured_run_loop_kwargs: list | None = None,
    captured_log_bridge_calls: list | None = None,
    captured_filter_calls: list | None = None,
) -> None:
    """Stub the side-effecting parts of cli.run so a CliRunner invocation
    completes synchronously without touching the network or main loop.

    Leaves real: argv parsing, derive_fn selection, dry-run resolution,
    --verbose-hashiverse / --verbose-filtering gating, the call to
    _start_clients_for_identities. That's the surface under test.
    """
    def capturing_start(identities, identity_dirs, global_salt, *, derive_fn):
        captured_derive_fns.append(derive_fn)
        return {}

    def capturing_run_loop(**kwargs):
        if captured_run_loop_kwargs is not None:
            captured_run_loop_kwargs.append(kwargs)

    def capturing_log_bridge() -> None:
        if captured_log_bridge_calls is not None:
            captured_log_bridge_calls.append(True)

    def capturing_set_verbose_filtering(enabled: bool) -> None:
        if captured_filter_calls is not None:
            captured_filter_calls.append(enabled)

    monkeypatch.setattr(cli, "_start_clients_for_identities", capturing_start)
    monkeypatch.setattr(cli, "run_loop", capturing_run_loop)
    monkeypatch.setattr(cli, "FileWatcher", _NoOpWatcher)
    # The Rust→Python log bridge is a process-wide one-shot; pytest invokes
    # cli.main repeatedly within one process, so stub it out per-test.
    monkeypatch.setattr(cli, "init_hashiverse_logging", capturing_log_bridge)
    # The picker's verbose-filtering flag is also process-wide module state;
    # stub the cli.set_verbose_filtering re-export so tests can capture
    # without leaking state into the picker module across tests.
    monkeypatch.setattr(cli, "set_verbose_filtering", capturing_set_verbose_filtering)
    monkeypatch.setenv("NEWS_AGENT_GLOBAL_SALT", "z" * 64)


def test_run_with_test_mode_uses_cheap_derive_fn(monkeypatch, tmp_path):
    """--test wires derive_keyphrase_cheap through to client startup."""
    captured: list = []
    _stub_cli_run_side_effects(monkeypatch, captured_derive_fns=captured)

    control_path = tmp_path / "control.yaml"
    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    result = CliRunner().invoke(
        cli.main, ["run", "--control", str(control_path), "--test"]
    )

    assert result.exit_code == 0, result.output
    assert captured == [derive_keyphrase_cheap]


def test_run_without_test_mode_uses_production_derive_fn(monkeypatch, tmp_path):
    """Without --test, derive_keyphrase (production) is wired through."""
    captured: list = []
    _stub_cli_run_side_effects(monkeypatch, captured_derive_fns=captured)
    # Path.home() drives daemon-dir resolution; redirect to tmp so the test
    # creates ~/.news-agent inside tmp_path rather than the developer's $HOME.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    control_path = tmp_path / "control.yaml"
    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    result = CliRunner().invoke(
        cli.main, ["run", "--control", str(control_path), "--create-new"]
    )

    assert result.exit_code == 0, result.output
    assert captured == [derive_keyphrase]


# ---------------------------------------------------------------------------
# Dry-run is the default; --production opts in to real posts; --test/--production are mutex.


def test_run_default_runs_in_dry_run_mode(monkeypatch, tmp_path):
    """No flags → dry_run=True is propagated to run_loop."""
    derive_fns: list = []
    run_loop_kwargs: list = []
    _stub_cli_run_side_effects(
        monkeypatch,
        captured_derive_fns=derive_fns,
        captured_run_loop_kwargs=run_loop_kwargs,
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    control_path = tmp_path / "control.yaml"
    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    result = CliRunner().invoke(
        cli.main, ["run", "--control", str(control_path), "--create-new"]
    )

    assert result.exit_code == 0, result.output
    assert len(run_loop_kwargs) == 1
    assert run_loop_kwargs[0]["dry_run"] is True


def test_run_with_production_flag_disables_dry_run(monkeypatch, tmp_path):
    """--production → dry_run=False is propagated to run_loop."""
    derive_fns: list = []
    run_loop_kwargs: list = []
    _stub_cli_run_side_effects(
        monkeypatch,
        captured_derive_fns=derive_fns,
        captured_run_loop_kwargs=run_loop_kwargs,
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    control_path = tmp_path / "control.yaml"
    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    result = CliRunner().invoke(
        cli.main,
        ["run", "--control", str(control_path), "--create-new", "--production"],
    )

    assert result.exit_code == 0, result.output
    assert run_loop_kwargs[0]["dry_run"] is False


def test_run_test_and_production_together_exits_nonzero(monkeypatch, tmp_path):
    """--test and --production are incoherent; the daemon must refuse to start."""
    derive_fns: list = []
    run_loop_kwargs: list = []
    _stub_cli_run_side_effects(
        monkeypatch,
        captured_derive_fns=derive_fns,
        captured_run_loop_kwargs=run_loop_kwargs,
    )

    control_path = tmp_path / "control.yaml"
    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    result = CliRunner().invoke(
        cli.main,
        ["run", "--control", str(control_path), "--test", "--production"],
    )

    assert result.exit_code != 0
    # Mutex check fires before any client/run-loop work happens.
    assert derive_fns == []
    assert run_loop_kwargs == []


def test_run_with_test_mode_runs_in_dry_run(monkeypatch, tmp_path):
    """--test alone → dry_run=True propagates (test mode forces dry-run)."""
    derive_fns: list = []
    run_loop_kwargs: list = []
    _stub_cli_run_side_effects(
        monkeypatch,
        captured_derive_fns=derive_fns,
        captured_run_loop_kwargs=run_loop_kwargs,
    )

    control_path = tmp_path / "control.yaml"
    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    result = CliRunner().invoke(
        cli.main, ["run", "--control", str(control_path), "--test"]
    )

    assert result.exit_code == 0, result.output
    assert run_loop_kwargs[0]["dry_run"] is True


# ---------------------------------------------------------------------------
# --verbose-hashiverse gates the Rust→Python log bridge


def test_run_without_verbose_hashiverse_skips_log_bridge(monkeypatch, tmp_path):
    """Default invocation does NOT install the Rust log bridge."""
    derive_fns: list = []
    log_bridge_calls: list = []
    _stub_cli_run_side_effects(
        monkeypatch,
        captured_derive_fns=derive_fns,
        captured_log_bridge_calls=log_bridge_calls,
    )

    control_path = tmp_path / "control.yaml"
    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    result = CliRunner().invoke(
        cli.main, ["run", "--control", str(control_path), "--test"]
    )

    assert result.exit_code == 0, result.output
    assert log_bridge_calls == []


def test_run_with_verbose_hashiverse_initializes_log_bridge(monkeypatch, tmp_path):
    """--verbose-hashiverse → init_hashiverse_logging is called once."""
    derive_fns: list = []
    log_bridge_calls: list = []
    _stub_cli_run_side_effects(
        monkeypatch,
        captured_derive_fns=derive_fns,
        captured_log_bridge_calls=log_bridge_calls,
    )

    control_path = tmp_path / "control.yaml"
    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    result = CliRunner().invoke(
        cli.main,
        ["run", "--control", str(control_path), "--test", "--verbose-hashiverse"],
    )

    assert result.exit_code == 0, result.output
    assert log_bridge_calls == [True]


# ---------------------------------------------------------------------------
# --verbose-filtering gates the picker's per-rejection log lines


def test_run_default_does_not_enable_verbose_filtering(monkeypatch, tmp_path):
    """No flag → set_verbose_filtering(False) is called once."""
    derive_fns: list = []
    filter_calls: list = []
    _stub_cli_run_side_effects(
        monkeypatch,
        captured_derive_fns=derive_fns,
        captured_filter_calls=filter_calls,
    )

    control_path = tmp_path / "control.yaml"
    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    result = CliRunner().invoke(
        cli.main, ["run", "--control", str(control_path), "--test"]
    )

    assert result.exit_code == 0, result.output
    assert filter_calls == [False]


def test_run_with_verbose_filtering_enables_picker_logging(monkeypatch, tmp_path):
    """--verbose-filtering → set_verbose_filtering(True) is called once."""
    derive_fns: list = []
    filter_calls: list = []
    _stub_cli_run_side_effects(
        monkeypatch,
        captured_derive_fns=derive_fns,
        captured_filter_calls=filter_calls,
    )

    control_path = tmp_path / "control.yaml"
    _write_control(control_path, _identity_block(SALT_A, "alpha"))

    result = CliRunner().invoke(
        cli.main,
        ["run", "--control", str(control_path), "--test", "--verbose-filtering"],
    )

    assert result.exit_code == 0, result.output
    assert filter_calls == [True]


# ---------------------------------------------------------------------------
# _ColorFormatter — wraps log-level names in ANSI escape codes on TTY stderr.


def _make_log_record(level: int, message: str = "msg") -> logging.LogRecord:
    return logging.LogRecord(
        name="news_agent.test",
        level=level,
        pathname=__file__,
        lineno=0,
        msg=message,
        args=(),
        exc_info=None,
    )


_COLOR_FMT = "%(color)s%(levelname)-7s%(color_reset)s %(message)s"


def test_color_formatter_wraps_warning_in_yellow():
    formatter = cli._ColorFormatter(_COLOR_FMT, use_color=True)
    out = formatter.format(_make_log_record(logging.WARNING, "watch out"))
    assert "\x1b[33m" in out  # yellow
    assert "\x1b[0m" in out
    assert "watch out" in out


def test_color_formatter_wraps_error_in_red():
    formatter = cli._ColorFormatter(_COLOR_FMT, use_color=True)
    out = formatter.format(_make_log_record(logging.ERROR))
    assert "\x1b[31m" in out  # red
    assert "\x1b[0m" in out


def test_color_formatter_critical_is_bold_red():
    formatter = cli._ColorFormatter(_COLOR_FMT, use_color=True)
    out = formatter.format(_make_log_record(logging.CRITICAL))
    assert "\x1b[1;91m" in out  # bold bright red
    assert "\x1b[0m" in out


def test_color_formatter_dim_grey_for_debug():
    formatter = cli._ColorFormatter(_COLOR_FMT, use_color=True)
    out = formatter.format(_make_log_record(logging.DEBUG))
    assert "\x1b[90m" in out  # gray


def test_color_formatter_leaves_info_uncolored():
    """INFO is the noisy level; coloring it would add visual clutter."""
    formatter = cli._ColorFormatter(_COLOR_FMT, use_color=True)
    out = formatter.format(_make_log_record(logging.INFO))
    assert "\x1b[" not in out


def test_color_formatter_no_codes_when_color_disabled():
    """use_color=False suppresses ANSI even at WARNING/ERROR."""
    formatter = cli._ColorFormatter(_COLOR_FMT, use_color=False)
    for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL):
        out = formatter.format(_make_log_record(level))
        assert "\x1b[" not in out, f"unexpected ANSI for level {level}: {out!r}"


def test_color_formatter_levelname_padding_preserved():
    """`%(levelname)-7s` width spec must still pad correctly when the color
    codes wrap the field — the codes sit *outside* the formatted span."""
    formatter = cli._ColorFormatter("[%(color)s%(levelname)-7s%(color_reset)s]", use_color=True)
    out = formatter.format(_make_log_record(logging.WARNING))
    # WARNING is 7 chars exactly → no padding spaces; the closing bracket
    # must immediately follow the reset code.
    assert "\x1b[0m]" in out

    out_info = formatter.format(_make_log_record(logging.INFO))
    # INFO is 4 chars → 3 trailing spaces inside the level span.
    # With color disabled at INFO, the brackets bracket the padded levelname.
    assert "[INFO   ]" in out_info
