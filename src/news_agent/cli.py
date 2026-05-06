"""``news-agent`` CLI entrypoint.

Phase 1 scope: read the two files, set up the directory tree, watch for changes,
log when reloads happen. No keys, no hashiverse client, no networking.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from pathlib import Path

import click

from news_agent.config import ControlConfig, ControlFileError, load_control
from news_agent.data_dir import (
    DaemonDirMissingError,
    ensure_daemon_dir,
    ensure_identity_dirs,
)
from news_agent.global_salt import (
    GlobalSalt,
    MissingGlobalSaltError,
    load_global_salt,
)
from news_agent.opml_loader import FeedSpec, OpmlParseError, load_opml
from news_agent.runtime_state import RuntimeSnapshot, RuntimeState
from news_agent.watcher import FileWatcher

logger = logging.getLogger("news_agent")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load_inputs(
    feeds_path: Path, control_path: Path
) -> tuple[tuple[FeedSpec, ...], ControlConfig]:
    feeds = tuple(load_opml(feeds_path))
    control = load_control(control_path)
    return feeds, control


def _summary(feeds: tuple[FeedSpec, ...], control: ControlConfig) -> str:
    return (
        f"loaded {len(feeds)} feed(s) and "
        f"{len(control.identities)} valid identity/identities"
    )


@click.group()
def main() -> None:
    """news-agent — forkable RSS-to-hashiverse mirroring daemon."""


@main.command()
@click.option(
    "--feeds",
    "feeds_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the OPML feeds file.",
)
@click.option(
    "--control",
    "control_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the YAML control file.",
)
@click.option(
    "--create-new",
    is_flag=True,
    help="Create the per-daemon data directory if it doesn't already exist.",
)
def run(feeds_path: Path, control_path: Path, create_new: bool) -> None:
    """Start the daemon. Watches both files for changes and reloads in place."""
    _configure_logging()

    try:
        salt = _load_global_salt_or_exit()
        daemon_dir = ensure_daemon_dir(salt, create_new=create_new)

        try:
            feeds, control = _load_inputs(feeds_path, control_path)
        except (OpmlParseError, ControlFileError) as exc:
            logger.error("could not load inputs: %s", exc)
            sys.exit(2)

        ensure_identity_dirs(daemon_dir, control.identities)
        state = RuntimeState(RuntimeSnapshot(feeds=feeds, control=control))
        logger.info("startup: %s; daemon dir = %s", _summary(feeds, control), daemon_dir)

        stop_event = threading.Event()

        def on_change(key: str) -> None:
            logger.info("%s file changed, reloading", key)
            try:
                new_feeds, new_control = _load_inputs(feeds_path, control_path)
            except (OpmlParseError, ControlFileError) as exc:
                logger.error(
                    "reload failed (%s file invalid): %s — keeping previous state",
                    key,
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
            state.swap(RuntimeSnapshot(feeds=new_feeds, control=new_control))
            logger.info("reload OK: %s", _summary(new_feeds, new_control))

        watcher = FileWatcher(feeds_path, control_path, on_change)
        watcher.start()
        logger.info(
            "watching %s and %s for changes (Ctrl-C to stop)",
            feeds_path,
            control_path,
        )

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
            # ValueError if not main thread; AttributeError if SIGINT missing.
            pass

        # Poll the stop event with a short timeout instead of blocking forever.
        # On Windows, an unbounded Event.wait() swallows Ctrl-C — Python only
        # checks for signals between bytecode steps, and time.sleep() yields
        # control in a way Event.wait() does not. Belt-and-braces: also catch
        # KeyboardInterrupt explicitly.
        try:
            while not stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("received Ctrl-C, shutting down")
        finally:
            watcher.stop()
            logger.info("stopped")
    except DaemonDirMissingError as exc:
        logger.error(str(exc))
        sys.exit(2)


def _load_global_salt_or_exit() -> GlobalSalt:
    try:
        return load_global_salt()
    except MissingGlobalSaltError as exc:
        logger.error(str(exc))
        sys.exit(2)


if __name__ == "__main__":
    main()
