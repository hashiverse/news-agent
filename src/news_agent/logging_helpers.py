"""Friendly-cranky log-line helpers, plus the random-salt suggestion generator.

Operators will see these messages when their salts are too short. The tone is
deliberately a bit chiding so the warnings are noticed rather than skimmed past.
"""

from __future__ import annotations

import secrets

MINIMUM_SALT_LENGTH = 32


def random_salt_suggestion(length: int = 64) -> str:
    """Return a path-safe, cryptographically-random salt suggestion.

    Uses URL-safe base64 (no `/` or `+`), trimmed to the requested length.
    Different on every call.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    raw_bytes_needed = (length * 3) // 4 + 3
    token = secrets.token_urlsafe(raw_bytes_needed)
    return token[:length]


def short_global_salt_warning(actual_length: int) -> str:
    """Build the warning line shown when NEWS_AGENT_GLOBAL_SALT is too short."""
    suggestion = random_salt_suggestion()
    return (
        f"hey, your NEWS_AGENT_GLOBAL_SALT is disgustingly short "
        f"({actual_length} chars, want at least {MINIMUM_SALT_LENGTH}). "
        f"this is unsafe. why don't you try something random like {suggestion}"
    )


def short_identity_salt_warning(identity_name: str, actual_length: int) -> str:
    """Build the warning line shown when an identity's local salt is too short."""
    suggestion = random_salt_suggestion()
    return (
        f"identity '{identity_name}' ignored — salt is too short to be safe "
        f"({actual_length} chars, want at least {MINIMUM_SALT_LENGTH}). "
        f"if you want, you can use {suggestion}"
    )
