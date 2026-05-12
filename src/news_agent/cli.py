"""``news-agent`` CLI entrypoint.

Reads a YAML control file (local path or HTTPS URL), sets up the per-daemon
data directory, brings up one hashiverse client per enabled identity, and
runs the scheduling-and-posting loop. The control file is watched for
changes (with periodic re-fetch when it's a remote URL); on change, the
in-memory state is fully rebuilt from disk.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import click
from hashiverse_client import init_logging as init_hashiverse_logging

from news_agent.config import ControlConfig, ControlFileError, IdentityConfig, load_control
from news_agent.data_dir import (
    DaemonDirMissingError,
    IdentityDir,
    ensure_cache_dir,
    ensure_daemon_dir,
    ensure_identity_dirs,
)
from news_agent.global_salt import (
    MissingGlobalSaltError,
    load_global_salt,
)
from news_agent.hashiverse_setup import start_hashiverse_client_for_identity
from news_agent.keyphrase import derive_keyphrase, derive_keyphrase_cheap
from news_agent.picker import set_verbose_filtering
from news_agent.poller import RemotePoller
from news_agent.remote_source import (
    CachedFile,
    FetchOutcome,
    fetch_to_cache,
    is_url,
    normalize_github_url,
)
from news_agent.runner import run_loop
from news_agent.runtime_state import RuntimeSnapshot, RuntimeState
from news_agent.state_db import open_state_db
from news_agent.watcher import FileWatcher

logger = logging.getLogger("news_agent")


class _ColorFormatter(logging.Formatter):
    """A logging.Formatter that wraps the level name in ANSI escape codes.

    INFO is intentionally left uncolored — it's the most common level, so
    coloring it would just add noise. DEBUG / WARNING / ERROR / CRITICAL
    each get a distinct color so non-INFO records pop visually in a long log.

    Color codes are inserted via two synthetic record attributes (``color`` /
    ``color_reset``) which the format string interpolates *around* the
    ``%(levelname)-7s`` span. Width-padding is therefore unaffected by the
    presence of escape codes.
    """

    _COLORS = {
        logging.DEBUG: "\x1b[90m",      # gray
        logging.WARNING: "\x1b[33m",    # yellow
        logging.ERROR: "\x1b[31m",      # red
        logging.CRITICAL: "\x1b[1;91m", # bold bright red
    }
    _RESET = "\x1b[0m"

    def __init__(self, fmt: str, *, use_color: bool) -> None:
        super().__init__(fmt)
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if self._use_color and record.levelno in self._COLORS:
            record.color = self._COLORS[record.levelno]
            record.color_reset = self._RESET
        else:
            record.color = ""
            record.color_reset = ""
        return super().format(record)


def _configure_logging(*, verbose_hashiverse: bool) -> None:
    # Color is opt-out via the no-color.org convention (NO_COLOR env var) and
    # opt-in only when stderr is a real terminal — pipes/files/journald don't
    # render escape codes, so emitting them there would just clutter output.
    use_color = sys.stderr.isatty() and "NO_COLOR" not in os.environ
    fmt = "%(asctime)s %(color)s%(levelname)-7s%(color_reset)s %(name)s: %(message)s"
    root = logging.getLogger()
    # Idempotency check matches logging.basicConfig: only attach a handler if
    # the root logger has none. Keeps repeated cli.run invocations under
    # pytest from piling up duplicate handlers (and duplicate output).
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_ColorFormatter(fmt, use_color=use_color))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    if verbose_hashiverse:
        # Bridge Rust hashiverse-client `log::*` output into Python's logging.
        # Process-wide one-shot; logger names on the Python side are the Rust
        # target (e.g. `hashiverse_lib::client::peer_tracker`). Off by default
        # because the Rust stack is chatty at trace/debug levels.
        init_hashiverse_logging()


def _summary(control: ControlConfig) -> str:
    total_sources = sum(len(i.sources) for i in control.identities)
    return (
        f"loaded {len(control.identities)} valid identity/identities "
        f"with {total_sources} source URL(s) total"
    )


def _resolve_control(
    arg: str, cache_dir: Path
) -> tuple[Path, str | None, CachedFile | None]:
    """Resolve the ``--control`` argument to a local path.

    URL: normalize (blob → raw), fetch into the cache, return cache path.
    Path: validate existence, return as-is.

    Returns ``(local_path, normalized_url_or_None, cached_file_or_None)``.
    """
    if is_url(arg):
        url = normalize_github_url(arg)
        if url != arg:
            logger.info("rewriting GitHub blob URL to raw: %s", url)
        cached = CachedFile.for_filename(cache_dir, "control.yaml")
        outcome = fetch_to_cache(url, cached)
        if outcome is FetchOutcome.NO_CACHE:
            raise click.ClickException(
                f"could not fetch {url} and no cache exists at {cached.path}"
            )
        if outcome is FetchOutcome.STALE:
            logger.warning(
                "starting with stale cache at %s (upstream fetch failed)", cached.path
            )
        return cached.path, url, cached

    path = Path(arg)
    if not path.is_file():
        raise click.ClickException(f"file not found: {path}")
    return path, None, None


@click.group()
def main() -> None:
    """news-agent — forkable RSS-to-hashiverse mirroring daemon."""


@main.command()
@click.option(
    "--control",
    "control_arg",
    type=str,
    required=True,
    help="Path to the YAML control file, OR an HTTPS URL (e.g. a raw.githubusercontent.com URL or a github.com/.../blob/... URL — blob URLs are auto-rewritten).",
)
@click.option(
    "--create-new",
    is_flag=True,
    help="Create the per-daemon data directory if it doesn't already exist.",
)
@click.option(
    "--remote-control-poll-minutes",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
    help="How often to re-fetch a URL-typed control file (minutes). Ignored for local-path control files.",
)
@click.option(
    "--test",
    "test_mode",
    is_flag=True,
    help="Run with an ephemeral home directory created in a temp path; deleted on exit. Implies --create-new and runs in dry-run mode. Mutually exclusive with --production. Useful for smoke tests so they don't leave debris in ~/.news-agent.",
)
@click.option(
    "--production",
    "production",
    is_flag=True,
    help="Post for real to hashiverse. Without this flag, the daemon runs in dry-run mode — logging what would have been posted instead. Dry-run posts ARE recorded in the posts-history table (with is_dry_run=1) so the scheduler still respects per-identity caps and cross-identity dedupe. Mutually exclusive with --test.",
)
@click.option(
    "--verbose-hashiverse",
    "verbose_hashiverse",
    is_flag=True,
    help="Bridge log output from the Rust hashiverse-client into Python's logging. Off by default because the Rust stack is chatty. Once on, lower the Python root logger level to surface DEBUG/TRACE Rust records.",
)
@click.option(
    "--verbose-filtering",
    "verbose_filtering",
    is_flag=True,
    help="Log every article rejected by the keyword filter at INFO level. Off by default — useful when tuning keywords_required / keywords_optional. Each rejection logs the missing/expected keywords and the haystack the picker compared against.",
)
def run(
    control_arg: str,
    create_new: bool,
    remote_control_poll_minutes: int,
    test_mode: bool,
    production: bool,
    verbose_hashiverse: bool,
    verbose_filtering: bool,
) -> None:
    """Start the daemon. Watches the control file for changes and reloads in place."""
    _configure_logging(verbose_hashiverse=verbose_hashiverse)

    # Plumb the picker's per-rejection logging flag from the CLI into the
    # picker module's process-wide toggle. Off by default so steady-state
    # operation isn't drowned in `keyword filter rejected ...` lines.
    set_verbose_filtering(verbose_filtering)

    # Mutex: --test always runs in dry-run; --production opts in to real posts.
    # Combining them is incoherent — fail fast rather than silently picking one.
    if test_mode and production:
        logger.error(
            "--test and --production are mutually exclusive; refusing to start"
        )
        sys.exit(2)

    # Dry-run is the default. Posting only happens when the operator explicitly
    # opts in with --production.
    dry_run = not production

    pollers: list[RemotePoller] = []
    watcher: FileWatcher | None = None
    ephemeral_home: Path | None = None

    # --test runs cheap argon2 (~50 ms/identity) instead of production
    # parameters (~1-2 s/identity). The cheap params are safe here because
    # --test implies an ephemeral home dir and dry-run mode (the mutex
    # check above ruled out --production).
    derive_fn: Callable[[str, str], str] = (
        derive_keyphrase_cheap if test_mode else derive_keyphrase
    )

    if test_mode:
        ephemeral_home = Path(tempfile.mkdtemp(prefix="news-agent-test-"))
        logger.info("test mode: using ephemeral home directory at %s", ephemeral_home)
        create_new = True  # the directory definitely doesn't exist yet
        # atexit registration is a belt-and-braces: it runs on normal exit,
        # SystemExit, and unhandled exceptions, even if the outer finally block
        # below didn't get a chance to run (e.g. signal-killed mid-syscall).
        atexit.register(_safe_rmtree, ephemeral_home)

    if dry_run:
        logger.info(
            "dry-run mode: nothing will be posted to the network "
            "(pass --production to post for real)"
        )
    else:
        logger.info("PRODUCTION mode: real posts will be made to hashiverse")

    try:
        try:
            salt = load_global_salt(home=ephemeral_home)
        except MissingGlobalSaltError as exc:
            logger.error(str(exc))
            sys.exit(2)

        daemon_dir = ensure_daemon_dir(salt, create_new=create_new)
        cache_dir = ensure_cache_dir(daemon_dir)

        control_path, control_url, control_cached = _resolve_control(
            control_arg, cache_dir
        )

        try:
            control = load_control(control_path)
        except ControlFileError as exc:
            logger.error("could not load control file: %s", exc)
            sys.exit(2)

        identity_dirs = ensure_identity_dirs(daemon_dir, control.identities)
        state = RuntimeState(RuntimeSnapshot(control=control))
        logger.info(
            "startup: %s; daemon dir = %s", _summary(control), daemon_dir
        )

        # Open (or create) the daemon-wide SQLite state DB. No schema yet —
        # tables are added by feature blocks as they land.
        state_db = open_state_db(daemon_dir)

        # Bring up one hashiverse client per enabled identity. First-run paths
        # do argon2 (slow); subsequent runs load the cached public key.
        clients = _start_clients_for_identities(
            control.identities,
            identity_dirs,
            salt.raw_value,
            derive_fn=derive_fn,
            dry_run=dry_run,
        )

        stop_event = threading.Event()
        # Set by on_change after a successful reload; the runner clears it
        # at the top of every iteration. Wakes up any in-progress
        # _wait_until / _interruptible_sleep so the runner can recompute
        # the next post against the freshly-loaded state.
        reload_event = threading.Event()

        def on_change() -> None:
            logger.info("control file changed, reloading")
            _reload_state(
                clients=clients,
                state=state,
                control_path=control_path,
                daemon_dir=daemon_dir,
                global_salt=salt.raw_value,
                derive_fn=derive_fn,
                dry_run=dry_run,
            )
            # Always set: even if _reload_state silently kept the previous
            # state (parse error), waking the runner is harmless — it'll
            # just re-evaluate against the same identities.
            reload_event.set()

        watcher = FileWatcher(control_path, on_change)
        watcher.start()
        logger.info(
            "watching %s for changes (Ctrl-C to stop)", control_path
        )

        # Spawn a poller if --control was a URL; the poller re-fetches the URL
        # on the configured interval, and a 200 OK rewrites the cache file,
        # which the watchdog observer then sees → reload pipeline runs.
        if control_url and control_cached:
            interval_seconds = remote_control_poll_minutes * 60.0

            def fetch_control() -> None:
                fetch_to_cache(control_url, control_cached)

            poller = RemotePoller(
                name="control",
                fetch_fn=fetch_control,
                stop_event=stop_event,
                interval_seconds=interval_seconds,
            )
            pollers.append(poller)
            poller.start()

        def _shutdown(signum, _frame):  # noqa: ANN001 — signal handler signature
            logger.info("received signal %s, shutting down", signum)
            stop_event.set()

        # SIGTERM triggers orderly shutdown via the stop event. SIGINT (Ctrl-C)
        # is intentionally LEFT WITH PYTHON'S DEFAULT HANDLER so it raises
        # KeyboardInterrupt: a custom handler that just calls stop_event.set()
        # is unreliable on Windows — when the main thread is in a long
        # stop_event.wait() the handler may not run promptly, and the daemon
        # appears unkillable. KeyboardInterrupt, by contrast, propagates
        # through Event.wait() cleanly on every platform Python supports
        # (the signal-aware wait was wired up in Python 3.5). The catch
        # below sets stop_event so the poller thread exits cleanly too.
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _shutdown)

        try:
            run_loop(
                state=state,
                clients=clients,
                conn=state_db,
                stop_event=stop_event,
                reload_event=reload_event,
                dry_run=dry_run,
            )
        except KeyboardInterrupt:
            logger.info("received Ctrl-C, shutting down")
            # Wake up any background threads (e.g. the URL-poller) sharing
            # this stop_event — without this they'd keep ticking until their
            # next wait timeout expires.
            stop_event.set()
        finally:
            for poller in pollers:
                poller.stop()
            if watcher is not None:
                watcher.stop()
            try:
                state_db.close()
            except Exception:  # noqa: BLE001 — best-effort close on shutdown
                logger.exception("closing state DB")
            # Drop hashiverse client refs so their internal runtimes can shut
            # down cleanly.
            clients.clear()
            logger.info("stopped")
    except DaemonDirMissingError as exc:
        logger.error(str(exc))
        sys.exit(2)
    finally:
        if ephemeral_home is not None:
            shutil.rmtree(ephemeral_home, ignore_errors=True)
            logger.info("test mode: removed ephemeral home %s", ephemeral_home)


@main.command(name="test-hashiverse")
@click.option(
    "--verbose-hashiverse",
    "verbose_hashiverse",
    is_flag=True,
    help="Bridge log output from the Rust hashiverse-client into Python's logging.",
)
def test_hashiverse(verbose_hashiverse: bool) -> None:
    """One-shot connectivity smoke test against the production hashiverse network.

    Builds a throwaway random identity (cheap argon2, tempdir data dir),
    fetches OG metadata from a fixed real URL, submits a single URL-preview
    post with the ``#test`` hashtag, then exits. No control file, no
    ``NEWS_AGENT_GLOBAL_SALT``, no identities required.
    """
    _configure_logging(verbose_hashiverse=verbose_hashiverse)
    # Local import keeps the daemon's startup path free of the smoke module
    # unless this subcommand is the one being invoked.
    from news_agent.hashiverse_smoke import run_hashiverse_smoke_test

    try:
        posted_url = run_hashiverse_smoke_test()
    except Exception:  # noqa: BLE001 — surface every failure as a clean exit code
        logger.exception("hashiverse smoke test failed")
        sys.exit(1)
    logger.info(
        "[OK] hashiverse smoke test complete - posted %s with #test", posted_url
    )


def _safe_rmtree(path: Path) -> None:
    """Remove a directory tree, ignoring errors. Suitable for atexit."""
    shutil.rmtree(path, ignore_errors=True)


def _reload_state(
    *,
    clients: dict[str, object],
    state: RuntimeState,
    control_path: Path,
    daemon_dir: Path,
    global_salt: str,
    derive_fn: Callable[[str, str], str],
    dry_run: bool,
) -> None:
    """Re-parse the control file and fully rebuild the in-memory state.

    Tear-down-and-rebuild on every reload. No diffing of the existing
    ``clients`` dict against the new identity set — instead, drop every
    existing client and re-run the same startup path that was used at
    daemon boot. Per-identity ``public_key.hex`` caches keep the cost
    cheap for unchanged identities (no argon2). Brand-new identities pay
    the argon2 cost; removed/disabled identities have their tokio runtimes
    released as their refs leave the dict.

    On failure (parse error, identity-dir creation failure), the existing
    state is left intact and the daemon keeps running with the previous
    configuration.
    """
    try:
        new_control = load_control(control_path)
    except ControlFileError as exc:
        logger.error(
            "reload failed (control file invalid): %s — keeping previous state",
            exc,
        )
        return
    try:
        new_identity_dirs = ensure_identity_dirs(daemon_dir, new_control.identities)
    except RuntimeError as exc:
        logger.error(
            "reload failed creating identity dirs: %s — keeping previous state",
            exc,
        )
        return

    # Tear down all existing clients. The dict is the same one the outer
    # scope holds, so clearing it here releases tokio runtimes inside the
    # now-orphaned clients.
    clients.clear()

    # Rebuild from disk. Cached public keys mean only brand-new identities
    # pay argon2.
    new_clients = _start_clients_for_identities(
        new_control.identities,
        new_identity_dirs,
        global_salt,
        derive_fn=derive_fn,
        dry_run=dry_run,
    )
    clients.update(new_clients)

    state.swap(RuntimeSnapshot(control=new_control))
    logger.info("reload OK: %s", _summary(new_control))


def _start_clients_for_identities(
    identities: tuple[IdentityConfig, ...],
    identity_dirs: list[IdentityDir],
    global_salt: str,
    *,
    derive_fn: Callable[[str, str], str],
    dry_run: bool,
) -> dict[str, object]:
    """Start one hashiverse client per enabled identity.

    Disabled identities are noted in the log and skipped — their data dir
    already exists, but the daemon won't bring up a client for them. Returns
    a mapping of ``identity.salt`` to the resulting client.

    ``dry_run`` is forwarded to ``start_hashiverse_client_for_identity`` so
    the per-identity bio sync gates ``set_bio`` calls behind production mode
    (dry-run logs would-be sends instead).
    """
    dirs_by_salt = {d.path.name: d for d in identity_dirs}
    clients: dict[str, object] = {}
    for identity in identities:
        identity_dir = dirs_by_salt[identity.salt]
        if not identity.enabled:
            logger.info(
                "skipping client startup for %s (enabled=false)", identity.log_label
            )
            continue
        client = start_hashiverse_client_for_identity(
            identity=identity,
            identity_dir=identity_dir.path,
            global_salt=global_salt,
            derive_fn=derive_fn,
            dry_run=dry_run,
        )
        clients[identity.salt] = client
        logger.info(
            "hashiverse client up for %s: client_id=%s",
            identity.log_label,
            client.client_id,
        )
    return clients


if __name__ == "__main__":
    main()
