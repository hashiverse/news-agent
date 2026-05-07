"""``news-agent`` CLI entrypoint.

Phase 2 scope: read the two files (local path or HTTPS URL), set up the
directory tree, watch for local-file changes, periodically re-fetch URL inputs
to detect upstream changes, log when reloads happen. No keys, no hashiverse
client interaction yet.
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
    ensure_cache_dir,
    ensure_daemon_dir,
    ensure_identity_dirs,
)
from news_agent.global_salt import (
    GlobalSalt,
    MissingGlobalSaltError,
    load_global_salt,
)
from news_agent.opml_loader import FeedSpec, OpmlParseError, load_opml
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


def _resolve_input(
    arg: str, cache_dir: Path, cache_filename: str
) -> tuple[Path, str | None, CachedFile | None]:
    """Resolve a CLI argument to a local path.

    If ``arg`` is an HTTPS URL: normalize it (blob → raw), fetch into the cache
    directory, return the cache path. If the initial fetch fails and no cache
    exists from a previous run, raise :class:`click.ClickException`.

    If ``arg`` is a local path: validate it exists, return it as-is.

    Returns ``(local_path, normalized_url_or_None, cached_file_or_None)``. The
    URL and cached-file objects are None for local paths, populated for URLs.
    """
    if is_url(arg):
        url = normalize_github_url(arg)
        if url != arg:
            logger.info("rewriting GitHub blob URL to raw: %s", url)
        cached = CachedFile.for_filename(cache_dir, cache_filename)
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
    "--feeds",
    "feeds_arg",
    type=str,
    required=True,
    help="Path to the OPML feeds file, OR an HTTPS URL (e.g. a raw.githubusercontent.com URL or a github.com/.../blob/... URL — blob URLs are auto-rewritten).",
)
@click.option(
    "--control",
    "control_arg",
    type=str,
    required=True,
    help="Path to the YAML control file, OR an HTTPS URL.",
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
    help="How often to re-fetch URL-typed inputs (minutes).",
)
def run(
    feeds_arg: str,
    control_arg: str,
    create_new: bool,
    remote_poll_minutes: int,
) -> None:
    """Start the daemon. Watches both files for changes and reloads in place."""
    _configure_logging()

    pollers: list[RemotePoller] = []
    watcher: FileWatcher | None = None

    try:
        salt = _load_global_salt_or_exit()
        daemon_dir = ensure_daemon_dir(salt, create_new=create_new)
        cache_dir = ensure_cache_dir(daemon_dir)

        feeds_path, feeds_url, feeds_cached = _resolve_input(
            feeds_arg, cache_dir, "feeds.opml"
        )
        control_path, control_url, control_cached = _resolve_input(
            control_arg, cache_dir, "control.yaml"
        )

        try:
            feeds, control = _load_inputs(feeds_path, control_path)
        except (OpmlParseError, ControlFileError) as exc:
            logger.error("could not load inputs: %s", exc)
            sys.exit(2)

        ensure_identity_dirs(daemon_dir, control.identities)
        state = RuntimeState(RuntimeSnapshot(feeds=feeds, control=control))
        logger.info(
            "startup: %s; daemon dir = %s", _summary(feeds, control), daemon_dir
        )

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

        # Spawn pollers for any URL-typed inputs. Each polls on the configured
        # interval; when it writes a new body to the cache file, the watchdog
        # observer fires and the on_change pipeline above runs.
        interval_seconds = remote_poll_minutes * 60.0
        if feeds_url and feeds_cached:
            pollers.append(
                _build_poller(
                    "feeds", feeds_url, feeds_cached, stop_event, interval_seconds
                )
            )
        if control_url and control_cached:
            pollers.append(
                _build_poller(
                    "control",
                    control_url,
                    control_cached,
                    stop_event,
                    interval_seconds,
                )
            )
        for poller in pollers:
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


def _build_poller(
    key: str,
    url: str,
    cached: CachedFile,
    stop_event: threading.Event,
    interval_seconds: float,
) -> RemotePoller:
    def fetch() -> None:
        fetch_to_cache(url, cached)

    return RemotePoller(
        name=key,
        fetch_fn=fetch,
        stop_event=stop_event,
        interval_seconds=interval_seconds,
    )


def _load_global_salt_or_exit() -> GlobalSalt:
    try:
        return load_global_salt()
    except MissingGlobalSaltError as exc:
        logger.error(str(exc))
        sys.exit(2)


if __name__ == "__main__":
    main()
