"""Stored-credential encryption at rest — AES-256-GCM (S2, ADR 0006).

Security properties (ADR 0006 §Security-decision): protects the database at
rest, backups, and ciphertext-only appearances. It does NOT defend backend
RCE, browser capture, or tenant isolation — those are separate controls.

Key management: a 32-byte master key referenced by *env-var name* in
Settings (D18 pattern — the value never enters config). `key_id` is an
8-byte fingerprint of the master key so a future rotation can tell which
key wrapped what; the versioned envelope `{v, key_id, nonce, ct}` leaves
room for KMS/re-wrap without a format rewrite. Tampering fails loudly.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12  # 96-bit, AES-GCM standard
_KEY_LEN = 32
_KEY_ID_LEN = 8
_VERSION = 1


class CryptoError(Exception):
    """Master key missing/invalid or ciphertext malformed/tampered."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def load_master_key(settings: object) -> bytes:
    """Read the master key from the env var *named* by settings
    (`credentials_master_key_env`). Base64url of exactly 32 bytes."""
    name = getattr(settings, "credentials_master_key_env", "")
    if not name:
        raise CryptoError("no master-key environment variable configured")
    raw = os.environ.get(name)
    if not raw:
        raise CryptoError(f"master key environment variable {name!r} is not set")
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"master key in {name!r} is not valid base64") from exc
    if len(key) != _KEY_LEN:
        raise CryptoError(f"master key in {name!r} must decode to exactly 32 bytes")
    return key


def key_id(key: bytes) -> str:
    """Short fingerprint of the master key — rotation-aware, not secret."""
    return hashlib.sha256(key).hexdigest()[: 2 * _KEY_ID_LEN]


def encrypt(secret: str, *, master_key: bytes) -> dict[str, str | int]:
    """AES-GCM encrypt; returns the JSONB-safe envelope {v, key_id, nonce, ct}
    (all base64url, no padding)."""
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(master_key).encrypt(nonce, secret.encode(), None)
    return {
        "v": str(_VERSION),
        "key_id": key_id(master_key),
        "nonce": base64.urlsafe_b64encode(nonce).decode().rstrip("="),
        "ct": base64.urlsafe_b64encode(ct).decode().rstrip("="),
    }


def decrypt(payload: dict[str, str | int], *, master_key: bytes) -> str:
    try:
        version = str(payload["v"])
        nonce_b64 = str(payload["nonce"])
        ct_b64 = str(payload["ct"])
        stored_key_id = str(payload["key_id"])
        if version != str(_VERSION):
            raise CryptoError(f"unsupported ciphertext version {version!r}")
        if stored_key_id != key_id(master_key):
            raise CryptoError(
                f"ciphertext was written under key_id {stored_key_id!r}, "
                "not the configured master key"
            )
        nonce = base64.urlsafe_b64decode(nonce_b64 + "=" * (-len(nonce_b64) % 4))
        ct = base64.urlsafe_b64decode(ct_b64 + "=" * (-len(ct_b64) % 4))
        try:
            return AESGCM(master_key).decrypt(nonce, ct, None).decode()
        except InvalidTag as exc:
            raise CryptoError("ciphertext failed authentication (tampered or wrong key)") from exc
    except CryptoError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise CryptoError(f"malformed ciphertext envelope: {exc}") from exc


__all__ = ["CryptoError", "decrypt", "encrypt", "key_id", "load_master_key"]
