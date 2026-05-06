"""Control-file (YAML) loader and per-identity validator.

Identities with structurally invalid config (missing required fields, wrong
types, short salt) are skipped with a log line and the rest of the daemon
keeps running with whatever's left.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from news_agent.logging_helpers import (
    MINIMUM_SALT_LENGTH,
    short_identity_salt_warning,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdentityConfig:
    """One hashiverse identity hosted by the daemon."""

    name: str
    salt: str
    description: str
    nickname: str
    status: str
    max_posts_per_day: int
    include_selectors: tuple[str, ...]
    exclude_selectors: tuple[str, ...] = ()
    exclude_urls: tuple[str, ...] = ()
    selfie: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class ControlConfig:
    """The fully-parsed control file."""

    identities: tuple[IdentityConfig, ...] = field(default_factory=tuple)


class ControlFileError(ValueError):
    """Raised when the control file is structurally unusable as a whole."""


_REQUIRED_STRING_FIELDS = ("name", "salt", "description", "nickname", "status")


def _coerce_str_list(value: Any, field_name: str, identity_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _IdentitySkip(
            identity_name,
            f"{field_name} must be a list of strings, got {type(value).__name__}",
        )
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise _IdentitySkip(
                identity_name,
                f"{field_name} contains non-string entry {entry!r}",
            )
        out.append(entry)
    return tuple(out)


class _IdentitySkip(Exception):
    """Internal signal: skip this identity and log why."""

    def __init__(self, identity_name: str, reason: str) -> None:
        super().__init__(f"identity {identity_name!r} skipped: {reason}")
        self.identity_name = identity_name
        self.reason = reason


def _build_identity(raw: Any) -> IdentityConfig:
    if not isinstance(raw, dict):
        raise _IdentitySkip(
            "<unnamed>", f"identity entry must be a mapping, got {type(raw).__name__}"
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _IdentitySkip("<unnamed>", "missing or empty 'name' field")
    name = name.strip()

    for field_name in _REQUIRED_STRING_FIELDS:
        value = raw.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise _IdentitySkip(name, f"missing or empty '{field_name}' field")

    salt = raw["salt"].strip()
    if len(salt) < MINIMUM_SALT_LENGTH:
        # Log the friendly-cranky line and skip — but don't raise to ControlFileError.
        logger.warning(short_identity_salt_warning(name, actual_length=len(salt)))
        raise _IdentitySkip(name, "salt too short")

    max_posts_per_day = raw.get("max_posts_per_day")
    if not isinstance(max_posts_per_day, int) or isinstance(max_posts_per_day, bool):
        raise _IdentitySkip(
            name, "'max_posts_per_day' must be an integer"
        )
    if max_posts_per_day < 0:
        raise _IdentitySkip(name, "'max_posts_per_day' must be >= 0")

    include_selectors = _coerce_str_list(
        raw.get("include_selectors"), "include_selectors", name
    )
    if not include_selectors:
        raise _IdentitySkip(
            name, "'include_selectors' is required and must be non-empty"
        )

    exclude_selectors = _coerce_str_list(
        raw.get("exclude_selectors"), "exclude_selectors", name
    )
    exclude_urls = _coerce_str_list(raw.get("exclude_urls"), "exclude_urls", name)

    enabled_raw = raw.get("enabled", True)
    if not isinstance(enabled_raw, bool):
        raise _IdentitySkip(name, "'enabled' must be a boolean")

    selfie_raw = raw.get("selfie")
    if selfie_raw is not None and not isinstance(selfie_raw, str):
        raise _IdentitySkip(name, "'selfie' must be a string (data URL)")

    return IdentityConfig(
        name=name,
        salt=salt,
        description=raw["description"].strip(),
        nickname=raw["nickname"].strip(),
        status=raw["status"].strip(),
        max_posts_per_day=max_posts_per_day,
        include_selectors=include_selectors,
        exclude_selectors=exclude_selectors,
        exclude_urls=exclude_urls,
        selfie=selfie_raw,
        enabled=enabled_raw,
    )


def load_control(path: Path) -> ControlConfig:
    """Parse the YAML control file. Returns the validated config.

    Identities that are structurally broken or have a too-short salt are
    skipped with a log line; valid ones are returned. Duplicate identity names
    cause the entire load to fail (the operator probably copy-pasted by mistake
    and we'd rather they notice than silently merge).
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ControlFileError(f"control file at {path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ControlFileError(f"could not read control file at {path}: {exc}") from exc

    if raw is None:
        return ControlConfig()
    if not isinstance(raw, dict):
        raise ControlFileError(
            f"control file at {path}: top-level must be a mapping"
        )

    raw_identities = raw.get("identities", [])
    if not isinstance(raw_identities, list):
        raise ControlFileError(
            f"control file at {path}: 'identities' must be a list"
        )

    seen_names: set[str] = set()
    identities: list[IdentityConfig] = []
    for entry in raw_identities:
        try:
            identity = _build_identity(entry)
        except _IdentitySkip as skip:
            if skip.reason != "salt too short":
                # The salt-too-short path already logs its own friendly-cranky line.
                logger.warning(
                    "identity %r ignored — %s",
                    skip.identity_name,
                    skip.reason,
                )
            continue

        if identity.name in seen_names:
            raise ControlFileError(
                f"duplicate identity name {identity.name!r} in {path}"
            )
        seen_names.add(identity.name)
        identities.append(identity)

    return ControlConfig(identities=tuple(identities))
