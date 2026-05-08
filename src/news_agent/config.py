"""Control-file (YAML) loader and per-identity validator.

Identities with structurally invalid config (missing required fields, wrong
types, short salt) are skipped with a log line and the rest of the daemon
keeps running with whatever's left. The loader also emits a soft warning
when the same RSS source URL appears in more than one identity's
``sources`` list — usually a config mistake (the same article would post
from multiple accounts), but legitimate uses exist so it stays a warning.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from news_agent.logging_helpers import (
    MINIMUM_SALT_LENGTH,
    short_identity_salt_warning,
)

logger = logging.getLogger(__name__)

SALT_PREFIX_LEN_FOR_LOGS = 8


@dataclass(frozen=True)
class IdentityConfig:
    """One hashiverse identity hosted by the daemon.

    The salt is the unique identifier (no separate ``name`` field). When
    referring to this identity in logs, use :attr:`log_label` so a short
    salt prefix is shown alongside the nickname for disambiguation.
    """

    salt: str
    nickname: str
    status: str
    max_posts_per_day: int
    sources: tuple[str, ...]
    selfie: str | None = None
    enabled: bool = True
    hashtags: tuple[str, ...] = ()
    # Case-insensitive substring filters against (title + summary).
    # - keywords_required: ALL must appear (AND). Empty → skip this check.
    # - keywords_optional: ANY must appear (OR). Empty → skip this check.
    # An identity with both fields empty (or absent) accepts all articles.
    # Stored lowercase at load time so the picker compares cheaply.
    keywords_required: tuple[str, ...] = ()
    keywords_optional: tuple[str, ...] = ()

    @property
    def log_label(self) -> str:
        return f"{self.nickname!r} (salt={self.salt[:SALT_PREFIX_LEN_FOR_LOGS]}…)"


@dataclass(frozen=True)
class ControlConfig:
    """The fully-parsed control file."""

    identities: tuple[IdentityConfig, ...] = field(default_factory=tuple)


class ControlFileError(ValueError):
    """Raised when the control file is structurally unusable as a whole."""


_REQUIRED_STRING_FIELDS = ("salt", "nickname", "status")


class _IdentitySkip(Exception):
    """Internal signal: skip this identity and log why."""

    def __init__(self, identity_label: str, reason: str) -> None:
        super().__init__(f"{identity_label} skipped: {reason}")
        self.identity_label = identity_label
        self.reason = reason


def _identity_label_for_diagnostics(raw: Any, index: int) -> str:
    """Best-effort label for an identity that may not have parsed yet."""
    if isinstance(raw, dict):
        nickname = raw.get("nickname")
        salt = raw.get("salt")
        nickname_part = (
            repr(nickname.strip())
            if isinstance(nickname, str) and nickname.strip()
            else "<no nickname>"
        )
        if isinstance(salt, str) and salt.strip():
            salt_prefix = salt.strip()[:SALT_PREFIX_LEN_FOR_LOGS]
            return f"identity #{index} {nickname_part} (salt={salt_prefix}…)"
        return f"identity #{index} {nickname_part}"
    return f"identity #{index} <not a mapping>"


def _coerce_str_list(
    value: Any, field_name: str, identity_label: str
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _IdentitySkip(
            identity_label,
            f"{field_name} must be a list of strings, got {type(value).__name__}",
        )
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise _IdentitySkip(
                identity_label,
                f"{field_name} contains non-string entry {entry!r}",
            )
        stripped = entry.strip()
        if not stripped:
            raise _IdentitySkip(
                identity_label, f"{field_name} contains an empty string"
            )
        out.append(stripped)
    return tuple(out)


def _build_identity(raw: Any, index: int) -> IdentityConfig:
    label = _identity_label_for_diagnostics(raw, index)

    if not isinstance(raw, dict):
        raise _IdentitySkip(
            label, f"identity entry must be a mapping, got {type(raw).__name__}"
        )

    for field_name in _REQUIRED_STRING_FIELDS:
        value = raw.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise _IdentitySkip(label, f"missing or empty '{field_name}' field")

    salt = raw["salt"].strip()
    nickname = raw["nickname"].strip()
    status = raw["status"].strip()

    if len(salt) < MINIMUM_SALT_LENGTH:
        # Use the friendly-cranky line, which already includes a fresh suggestion.
        logger.warning(short_identity_salt_warning(nickname, actual_length=len(salt)))
        raise _IdentitySkip(label, "salt too short")

    max_posts_per_day = raw.get("max_posts_per_day")
    if not isinstance(max_posts_per_day, int) or isinstance(max_posts_per_day, bool):
        raise _IdentitySkip(label, "'max_posts_per_day' must be an integer")
    if max_posts_per_day < 0:
        raise _IdentitySkip(label, "'max_posts_per_day' must be >= 0")

    sources = _coerce_str_list(raw.get("sources"), "sources", label)
    if not sources:
        raise _IdentitySkip(
            label, "'sources' is required and must contain at least one URL"
        )

    enabled_raw = raw.get("enabled", True)
    if not isinstance(enabled_raw, bool):
        raise _IdentitySkip(label, "'enabled' must be a boolean")

    selfie_raw = raw.get("selfie")
    if selfie_raw is not None and not isinstance(selfie_raw, str):
        raise _IdentitySkip(label, "'selfie' must be a string (data URL)")

    hashtags_raw = _coerce_str_list(raw.get("hashtags"), "hashtags", label)
    # Strip any leading '#' that slipped in — the prefix is added at post time.
    hashtags_stripped: list[str] = []
    for tag in hashtags_raw:
        cleaned = tag.lstrip("#")
        if not cleaned:
            raise _IdentitySkip(label, f"hashtags entry {tag!r} is empty after stripping '#'")
        hashtags_stripped.append(cleaned)
    hashtags = tuple(hashtags_stripped)

    # Lowercase keywords at load time so the picker matches against a
    # lower-cased haystack without re-lowering per article.
    required_raw = _coerce_str_list(raw.get("keywords_required"), "keywords_required", label)
    keywords_required = tuple(kw.lower() for kw in required_raw)
    optional_raw = _coerce_str_list(raw.get("keywords_optional"), "keywords_optional", label)
    keywords_optional = tuple(kw.lower() for kw in optional_raw)

    return IdentityConfig(
        salt=salt,
        nickname=nickname,
        status=status,
        max_posts_per_day=max_posts_per_day,
        sources=sources,
        selfie=selfie_raw,
        enabled=enabled_raw,
        hashtags=hashtags,
        keywords_required=keywords_required,
        keywords_optional=keywords_optional,
    )


def _warn_on_cross_identity_duplicates(
    identities: tuple[IdentityConfig, ...],
) -> None:
    """Emit a warning for each source URL appearing in two or more identities.

    Within-identity duplication (the same URL listed twice under one identity)
    does NOT trigger the warning — that's a different concern. Only cross-
    identity overlap is interesting here, and it's only a soft warning because
    legitimate uses exist (e.g. a global firehose plus a topic-narrow audience).
    """
    by_url: dict[str, set[IdentityConfig]] = defaultdict(set)
    for identity in identities:
        for source in identity.sources:
            by_url[source].add(identity)
    for url, holders in by_url.items():
        if len(holders) >= 2:
            labels = ", ".join(h.log_label for h in holders)
            logger.warning(
                "source %s appears in %d identities (%s) — same content will post from each",
                url,
                len(holders),
                labels,
            )


def load_control(path: Path) -> ControlConfig:
    """Parse the YAML control file and return the validated config.

    Identities that are structurally broken or have a too-short salt are
    skipped with a log line; valid ones are returned. Duplicate salts cause
    the entire load to fail (two identities with the same salt would derive
    the same hashiverse keys — almost certainly a copy-paste error).
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

    seen_salts: set[str] = set()
    identities: list[IdentityConfig] = []
    for index, entry in enumerate(raw_identities):
        try:
            identity = _build_identity(entry, index)
        except _IdentitySkip as skip:
            if skip.reason != "salt too short":
                # The salt-too-short path already logs its own friendly-cranky line.
                logger.warning("%s ignored — %s", skip.identity_label, skip.reason)
            continue

        if identity.salt in seen_salts:
            raise ControlFileError(
                f"duplicate salt in {path}: {identity.salt[:SALT_PREFIX_LEN_FOR_LOGS]}… "
                f"appears more than once. each identity must have a unique salt."
            )
        seen_salts.add(identity.salt)
        identities.append(identity)

    result = tuple(identities)
    _warn_on_cross_identity_duplicates(result)
    return ControlConfig(identities=result)
