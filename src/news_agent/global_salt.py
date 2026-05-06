"""Global-salt handling: read the env var, hash it, resolve the daemon directory."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import blake3

from news_agent.logging_helpers import (
    MINIMUM_SALT_LENGTH,
    short_global_salt_warning,
)

GLOBAL_SALT_ENV_VAR = "NEWS_AGENT_GLOBAL_SALT"
DAEMON_HOME_DIR_NAME = ".news-agent"

logger = logging.getLogger(__name__)


class MissingGlobalSaltError(RuntimeError):
    """Raised when the NEWS_AGENT_GLOBAL_SALT env var is unset or empty."""


@dataclass(frozen=True)
class GlobalSalt:
    """A loaded global salt and the derived daemon-level paths."""

    raw_value: str
    daemon_dir: Path

    @property
    def is_short(self) -> bool:
        return len(self.raw_value) < MINIMUM_SALT_LENGTH


def _blake3_hex(value: str) -> str:
    return blake3.blake3(value.encode("utf-8")).hexdigest()


def daemon_home_root(home: Path | None = None) -> Path:
    """Return ``~/.news-agent`` (or under an explicit home for tests)."""
    return (home or Path.home()) / DAEMON_HOME_DIR_NAME


def load_global_salt(home: Path | None = None) -> GlobalSalt:
    """Read the global salt from the environment, log a warning if it's short.

    Returns a :class:`GlobalSalt` carrying the resolved per-daemon directory.
    Raises :class:`MissingGlobalSaltError` if the env var is unset or empty —
    without a salt we can't even pick a directory to live in.
    """
    raw_value = os.environ.get(GLOBAL_SALT_ENV_VAR, "")
    if not raw_value:
        raise MissingGlobalSaltError(
            f"environment variable {GLOBAL_SALT_ENV_VAR} is not set; "
            f"news-agent needs it to namespace its data directory."
        )

    daemon_dir = daemon_home_root(home) / _blake3_hex(raw_value)
    salt = GlobalSalt(raw_value=raw_value, daemon_dir=daemon_dir)

    if salt.is_short:
        logger.warning(short_global_salt_warning(actual_length=len(raw_value)))

    return salt
