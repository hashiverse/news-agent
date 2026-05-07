"""Per-daemon and per-identity directory management.

The daemon-level directory is ``~/.news-agent/<blake3(NEWS_AGENT_GLOBAL_SALT)>/``.
If it doesn't exist on startup, the operator must pass ``--create-new`` to
acknowledge that they're spinning up a fresh daemon (vs. silently orphaning the
old one because of a typo in the global salt).

Each identity gets a per-identity subdirectory named after its local salt.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from news_agent.config import IdentityConfig
from news_agent.global_salt import GlobalSalt

logger = logging.getLogger(__name__)


class DaemonDirMissingError(RuntimeError):
    """Raised when the daemon directory doesn't exist and --create-new wasn't passed."""


@dataclass(frozen=True)
class IdentityDir:
    """A per-identity directory under the daemon root."""

    identity_label: str    # for logs — built from the identity's IdentityConfig.log_label
    path: Path


def ensure_daemon_dir(salt: GlobalSalt, *, create_new: bool) -> Path:
    """Make sure the daemon-level directory exists; create it iff allowed.

    Returns the path. Raises :class:`DaemonDirMissingError` if the directory
    doesn't exist and ``create_new`` is False.
    """
    if salt.daemon_dir.exists():
        if not salt.daemon_dir.is_dir():
            raise DaemonDirMissingError(
                f"{salt.daemon_dir} exists but is not a directory"
            )
        if create_new:
            logger.info(
                "daemon directory already exists at %s; --create-new is a no-op here",
                salt.daemon_dir,
            )
        return salt.daemon_dir

    if not create_new:
        raise DaemonDirMissingError(
            f"daemon directory {salt.daemon_dir} does not exist. "
            f"if this is the first run for this NEWS_AGENT_GLOBAL_SALT, "
            f"pass --create-new on the command line. "
            f"otherwise, check that NEWS_AGENT_GLOBAL_SALT matches the value "
            f"used previously."
        )

    salt.daemon_dir.mkdir(parents=True, exist_ok=False)
    logger.info("created new daemon directory at %s", salt.daemon_dir)
    return salt.daemon_dir


def ensure_cache_dir(daemon_dir: Path) -> Path:
    """Create (idempotently) the cache subdirectory under the daemon directory.

    Used to hold remote-fetched feed/control file content and their meta
    sidecars when ``--feeds`` or ``--control`` is an HTTPS URL.
    """
    cache_dir = daemon_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def ensure_identity_dirs(
    daemon_dir: Path, identities: Iterable[IdentityConfig]
) -> list[IdentityDir]:
    """Create per-identity subdirectories under the daemon directory.

    Idempotent — existing dirs are left alone. Returns the list of resolved
    per-identity paths in input order.
    """
    result: list[IdentityDir] = []
    for identity in identities:
        path = daemon_dir / identity.salt
        if path.exists():
            if not path.is_dir():
                raise RuntimeError(
                    f"identity dir {path} exists but is not a directory"
                )
        else:
            path.mkdir(parents=True, exist_ok=False)
            logger.info(
                "created identity directory for %s at %s",
                identity.log_label,
                path,
            )
        result.append(IdentityDir(identity_label=identity.log_label, path=path))
    return result
