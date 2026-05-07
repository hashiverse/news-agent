"""``news-agent`` CLI entrypoint.

Reads a YAML control file (local path or HTTPS URL), sets up the per-daemon
data directory, and watches the control file for changes (with periodic
re-fetch when it's a remote URL). No keys, no hashiverse client interaction
yet — those land in later phases.
"""

from __future__ import annotations

import atexit
import logging
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

import click

from news_agent.config import ControlConfig, ControlFileError, load_control
from news_agent.data_dir import (
    DaemonDirMissingError,
    ensure_cache_dir,
    ensure_daemon_dir,
    ensure_identity_dirs,
)
from news_agent.global_salt import (
    MissingGlobalSaltError,
    load_global_salt,
)
from news_agent.poller import RemotePoller
from news_agent.remote_source import (
    CachedFile,
    FetchOutcome,
    fetch_to_cache,
    is_url,
    normalize_github_url,
)
from news_agent.runtime_state import RuntimeSnapshot, RuntimeState
from news_agent.watcher import FileWatcher

logger = logging.getLogger("news_agent")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


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
    "--remote-poll-minutes",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
    help="How often to re-fetch a URL-typed control file (minutes). Ignored for local-path control files.",
)
@click.option(
    "--test",
    "test_mode",
    is_flag=True,
    help="Run with an ephemeral home directory created in a temp path; deleted on exit. Implies --create-new. Useful for smoke tests so they don't leave debris in ~/.news-agent.",
)
def run(
    control_arg: str,
    create_new: bool,
    remote_poll_minutes: int,
    test_mode: bool,
) -> None:
    """Start the daemon. Watches the control file for changes and reloads in place."""
    _configure_logging()

    pollers: list[RemotePoller] = []
    watcher: FileWatcher | None = None
    ephemeral_home: Path | None = None

    if test_mode:
        ephemeral_home = Path(tempfile.mkdtemp(prefix="news-agent-test-"))
        logger.info("test mode: using ephemeral home directory at %s", ephemeral_home)
        create_new = True  # the directory definitely doesn't exist yet
        # atexit registration is a belt-and-braces: it runs on normal exit,
        # SystemExit, and unhandled exceptions, even if the outer finally block
        # below didn't get a chance to run (e.g. signal-killed mid-syscall).
        atexit.register(_safe_rmtree, ephemeral_home)

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

        ensure_identity_dirs(daemon_dir, control.identities)
        state = RuntimeState(RuntimeSnapshot(control=control))
        logger.info(
            "startup: %s; daemon dir = %s", _summary(control), daemon_dir
        )

        stop_event = threading.Event()

        def on_change() -> None:
            logger.info("control file changed, reloading")
            try:
                new_control = load_control(control_path)
            except ControlFileError as exc:
                logger.error(
                    "reload failed (control file invalid): %s — keeping previous state",
                    exc,
                )
                return
            try:
                ensure_identity_dirs(daemon_dir, new_control.identities)
            except RuntimeError as exc:
                logger.error(
                    "reload failed creating identity dirs: %s — keeping previous state",
                    exc,
                )
                return
            state.swap(RuntimeSnapshot(control=new_control))
            logger.info("reload OK: %s", _summary(new_control))

        watcher = FileWatcher(control_path, on_change)
        watcher.start()
        logger.info(
            "watching %s for changes (Ctrl-C to stop)", control_path
        )

        # Spawn a poller if --control was a URL; the poller re-fetches the URL
        # on the configured interval, and a 200 OK rewrites the cache file,
        # which the watchdog observer then sees → reload pipeline runs.
        if control_url and control_cached:
            interval_seconds = remote_poll_minutes * 60.0

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

        # SIGTERM (and SIGINT, where the OS delivers it via signal handler) trigger
        # an orderly shutdown by setting the stop event. SIGINT on Windows is
        # better handled via KeyboardInterrupt below.
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _shutdown)
        try:
            signal.signal(signal.SIGINT, _shutdown)
        except (AttributeError, ValueError):
            pass

        try:
            while not stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("received Ctrl-C, shutting down")
        finally:
            for poller in pollers:
                poller.stop()
            if watcher is not None:
                watcher.stop()
            logger.info("stopped")
    except DaemonDirMissingError as exc:
        logger.error(str(exc))
        sys.exit(2)
    finally:
        if ephemeral_home is not None:
            shutil.rmtree(ephemeral_home, ignore_errors=True)
            logger.info("test mode: removed ephemeral home %s", ephemeral_home)


def _safe_rmtree(path: Path) -> None:
    """Remove a directory tree, ignoring errors. Suitable for atexit."""
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    main()
