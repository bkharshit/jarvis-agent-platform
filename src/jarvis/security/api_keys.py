"""API keys — generate, hash, display-prefix (ADR 0009 §2).

A key is ``jarvis_sk_<32 hex>``; only the SHA-256 hash and a short display
prefix are persisted. Plaintext is returned exactly once at creation and
never again (the write-only pattern of ADR 0006 §7, applied to keys).
"""

from __future__ import annotations

import hashlib
import secrets

_PREFIX = "jarvis_sk"
_HEX_LEN = 32


def generate_api_key() -> str:
    return f"{_PREFIX}_{secrets.token_hex(_HEX_LEN)}"


def is_api_key(token: str) -> bool:
    return token.startswith(f"{_PREFIX}_")


def hash_api_key(key: str) -> str:
    """SHA-256 hex of the exact key string — a stolen database yields no keys."""
    return hashlib.sha256(key.encode()).hexdigest()


def key_prefix(key: str) -> str:
    """Display prefix for list views (first 12 chars + ellipsis marker)."""
    return key[:12]


__all__ = ["generate_api_key", "hash_api_key", "is_api_key", "key_prefix"]
