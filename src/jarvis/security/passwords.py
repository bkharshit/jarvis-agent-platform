"""Password hashing — stdlib scrypt, no new dependency (ADR 0009 §2).

Format-versioned string: ``scrypt$n$r$p$salt_b64$hash_b64``. Verification
is constant-time. A stolen database yields no passwords: scrypt is
memory-hard and each hash carries its own random salt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

# OWASP-recommended scrypt parameters (n=2^14, r=8, p=1 → 16 MiB memory).
_N = 2**14
_R = 8
_P = 1
_DKLEN = 32
_SALT_LEN = 16
_PREFIX = "scrypt"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_LEN)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"{_PREFIX}${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        prefix, n, r, p, salt_b64, hash_b64 = stored.split("$")
    except ValueError:
        return False
    if prefix != _PREFIX:
        return False
    try:
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
        digest = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


__all__ = ["hash_password", "verify_password"]
