"""Keyphrase derivation: blake3 mix of (global_salt, local_salt) → argon2id → keyphrase string.

The argon2id output is what's handed to ``HashiverseClient.create_from_keyphrase``.
Production parameters are deliberately aggressive (1 GiB memory, 4 iterations) so
that a leak of one identity's derived keyphrase plus the control file does not
cheaply yield ``NEWS_AGENT_GLOBAL_SALT``. This is run at most once per identity
over the daemon's lifetime — the resulting public key is then cached on disk and
subsequent restarts go through the public-key fast path.

Tests can pass cheaper parameters via the optional kwargs.
"""

from __future__ import annotations

import blake3
from argon2.low_level import Type, hash_secret_raw

# Production parameters — locked in by Block 1.
ARGON2_MEMORY_KIB = 1024 * 1024  # 1 GiB
ARGON2_TIME_COST = 4
ARGON2_PARALLELISM = 1
ARGON2_OUTPUT_BYTES = 64

# Domain-separator salt for argon2. Argon2 requires a salt parameter; per-identity
# uniqueness already lives in the blake3-mixed secret, so this constant is fine
# (it's not a security boundary).
APP_DOMAIN_SALT = b"news-agent-v1"


def derive_keyphrase(
    global_salt: str,
    local_salt: str,
    *,
    memory_kib: int = ARGON2_MEMORY_KIB,
    time_cost: int = ARGON2_TIME_COST,
    parallelism: int = ARGON2_PARALLELISM,
    output_bytes: int = ARGON2_OUTPUT_BYTES,
    domain_salt: bytes = APP_DOMAIN_SALT,
) -> str:
    """Derive an identity's hashiverse keyphrase from the two salts.

    The returned string is hex-encoded argon2 output, suitable as the
    ``key_phrase`` argument to ``HashiverseClient.create_from_keyphrase``.

    Tests pass cheap parameter values to keep runs fast; production callers
    use the locked-in defaults.
    """
    if not global_salt:
        raise ValueError("global_salt must be non-empty")
    if not local_salt:
        raise ValueError("local_salt must be non-empty")

    hasher = blake3.blake3()
    hasher.update(global_salt.encode("utf-8"))
    hasher.update(local_salt.encode("utf-8"))
    blake_digest = hasher.digest()

    raw = hash_secret_raw(
        secret=blake_digest,
        salt=domain_salt,
        time_cost=time_cost,
        memory_cost=memory_kib,
        parallelism=parallelism,
        hash_len=output_bytes,
        type=Type.ID,
    )
    return raw.hex()
