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
    bootstrap_addresses: list[str] | None = None,
    client_factory: _ClientFactory = HashiverseClient,
    derive_fn: Callable[[str, str], str] = derive_keyphrase,
) -> Any:
    """Bring up one identity's hashiverse client.

    On first run for this identity (no cached client_id on disk), runs argon2
    to derive the keyphrase, builds a fresh client, and persists the resulting
    client_id. On subsequent runs, loads the cached client_id and uses the
    stored-key constructor — no argon2.

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
        return client_factory.create_from_stored_key(
            client_id_hex=client_id,
            data_dir=str(identity_dir),
            passphrase=global_salt,
            bootstrap_addresses=bootstrap_addresses,
        )

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
    return client


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
