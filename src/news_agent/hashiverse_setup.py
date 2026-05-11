"""Per-identity hashiverse client construction.

For a given identity, this module either:

- loads the identity's hashiverse client from disk via the cached client_id
  (fast path — no argon2), or
- runs argon2 once on the (global, local) salt pair, hands the derived
  keyphrase to ``HashiverseClient.create_from_keyphrase``, then writes the
  resulting client_id to ``<identity_dir>/client_id.hex`` for next time.

The hashiverse client is created with ``passphrase=NEWS_AGENT_GLOBAL_SALT``
so the on-disk key locker is encrypted under the same secret that drives
identity derivation. Single secret, three roles (per Block 1 of the plan).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from hashiverse_client import HashiverseClient

from news_agent.config import IdentityConfig
from news_agent.keyphrase import derive_keyphrase

logger = logging.getLogger(__name__)

CLIENT_ID_FILENAME = "client_id.hex"
LAST_BIO_FILENAME = "last_bio.json"
_BIO_FIELDS = ("nickname", "status", "selfie", "avatar")


class _ClientFactory(Protocol):
    """Subset of the HashiverseClient class used by this module.

    Carved out so tests can substitute a fake implementation without monkey-
    patching the real hashiverse client (which spawns a tokio runtime and
    network transport on construction).
    """

    @staticmethod
    def create_from_keyphrase(
        key_phrase: str,
        data_dir: str,
        passphrase: str = "",
        bootstrap_addresses: list[str] | None = None,
    ) -> Any: ...

    @staticmethod
    def create_from_stored_key(
        client_id_hex: str,
        data_dir: str,
        passphrase: str = "",
        bootstrap_addresses: list[str] | None = None,
    ) -> Any: ...


def start_hashiverse_client_for_identity(
    identity: IdentityConfig,
    identity_dir: Path,
    global_salt: str,
    *,
    dry_run: bool,
    bootstrap_addresses: list[str] | None = None,
    client_factory: _ClientFactory = HashiverseClient,
    derive_fn: Callable[[str, str], str] = derive_keyphrase,
) -> Any:
    """Bring up one identity's hashiverse client and sync its bio.

    On first run for this identity (no cached client_id on disk), runs argon2
    to derive the keyphrase, builds a fresh client, and persists the resulting
    client_id. On subsequent runs, loads the cached client_id and uses the
    stored-key constructor — no argon2.

    Then syncs the identity's bio to hashiverse via
    :func:`update_bio_if_changed`, gated on ``dry_run`` — production sends,
    dry-run logs only. Bio-sync failures are logged but do NOT block client
    construction; the next reload/restart retries.

    The ``client_factory`` and ``derive_fn`` parameters exist so tests can
    substitute fakes without mocking module-level imports.
    """
    client_id_path = identity_dir / CLIENT_ID_FILENAME

    if client_id_path.exists():
        client_id = client_id_path.read_text(encoding="utf-8").strip()
        if not client_id:
            raise RuntimeError(
                f"cached client_id file at {client_id_path} is empty; "
                f"delete it to force re-derivation"
            )
        logger.info(
            "loading hashiverse client for %s from cached client_id (no argon2)",
            identity.log_label,
        )
        client = client_factory.create_from_stored_key(
            client_id_hex=client_id,
            data_dir=str(identity_dir),
            passphrase=global_salt,
            bootstrap_addresses=bootstrap_addresses,
        )
        _try_update_bio(client, identity, identity_dir, dry_run=dry_run)
        return client

    logger.info(
        "first run for %s — deriving keyphrase via argon2 (this is slow on purpose)",
        identity.log_label,
    )
    derive_started = time.monotonic()
    keyphrase = derive_fn(global_salt, identity.salt)
    derive_elapsed = time.monotonic() - derive_started
    logger.info(
        "argon2 derivation for %s took %.1fs", identity.log_label, derive_elapsed
    )

    client = client_factory.create_from_keyphrase(
        key_phrase=keyphrase,
        data_dir=str(identity_dir),
        passphrase=global_salt,
        bootstrap_addresses=bootstrap_addresses,
    )

    client_id = client.client_id
    _atomic_write_text(client_id_path, client_id)
    logger.info(
        "persisted client_id for %s to %s", identity.log_label, client_id_path
    )
    _try_update_bio(client, identity, identity_dir, dry_run=dry_run)
    return client


def _try_update_bio(
    client: Any, identity: IdentityConfig, identity_dir: Path, *, dry_run: bool
) -> None:
    """Wrapper around update_bio_if_changed that swallows network failures.

    A transient set_bio error (e.g., unreachable peer at startup) shouldn't
    block the client from being usable for posting. The next reload or
    restart re-runs the bio sync.
    """
    try:
        update_bio_if_changed(client, identity, identity_dir, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 — bio failure shouldn't block startup
        logger.warning(
            "%s set_bio failed (%s) — client up, will retry on next reload/restart",
            identity.log_label, exc,
        )


def update_bio_if_changed(
    client: Any,
    identity: IdentityConfig,
    identity_dir: Path,
    *,
    dry_run: bool,
) -> bool:
    """Send identity bio to hashiverse iff it differs from what we last sent.

    Returns True if a network call (``client.set_bio``) was made, False if
    the cached value matched OR we were in dry-run mode. hashiverse emits a
    fresh meta-post on every ``set_bio`` call regardless of payload, so we
    de-dupe at the daemon layer to prevent redundant meta-posts on every
    restart and reload.

    Dry-run mode logs what would have been sent and skips the actual
    ``client.set_bio`` call — but the cache file IS updated, so a subsequent
    dry-run reload stays silent (matching the production steady state).
    This makes dry-run a faithful preview of the production state machine;
    the only divergence is the network side effect.
    """
    current = _current_bio_dict(identity)
    cached = _load_last_bio(identity_dir)
    if cached == current:
        logger.debug(
            "%s bio unchanged since last send — skipping set_bio",
            identity.log_label,
        )
        return False

    # Bio differs. In dry-run we still record what production would have sent
    # so subsequent reloads stay quiet and the on-disk state matches what
    # production would produce — only the network call is skipped.
    if dry_run:
        logger.info(
            "[DRY-RUN] %s would send bio update: %r",
            identity.log_label, current,
        )
    else:
        logger.info("%s sending bio update", identity.log_label)
        client.set_bio(
            current["nickname"],
            current["status"],
            current["selfie"],
            current["avatar"],
        )

    try:
        _save_last_bio(identity_dir, current)
    except OSError as exc:
        # Cache write failure means the next start will redundantly resend
        # (or re-log under dry-run). Annoying but not broken — flag it.
        logger.warning(
            "%s bio %s but writing %s failed: %s — will re-fire next start",
            identity.log_label,
            "would-send recorded" if dry_run else "sent",
            LAST_BIO_FILENAME, exc,
        )
    return not dry_run


def _current_bio_dict(identity: IdentityConfig) -> dict[str, str]:
    return {
        "nickname": identity.nickname,
        "status": identity.status,
        "selfie": identity.selfie or "",
        "avatar": "",  # IdentityConfig has no avatar field today
    }


def _load_last_bio(identity_dir: Path) -> dict[str, str] | None:
    """Return the previously-sent bio, or None if cache is missing/unparseable.

    A corrupt or partial cache is treated as missing — better to send a
    redundant bio than to silently skip a real update because of a bad
    cache file.
    """
    path = identity_dir / LAST_BIO_FILENAME
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read %s, treating as no cache: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, str] = {}
    for key in _BIO_FIELDS:
        value = data.get(key)
        if not isinstance(value, str):
            return None
        out[key] = value
    return out


def _save_last_bio(identity_dir: Path, bio: dict[str, str]) -> None:
    path = identity_dir / LAST_BIO_FILENAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(bio, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
