"""Security primitives (S2): password hashing, API keys, credential crypto.

Pure functions over stdlib + `cryptography` — no IO, no config, fully
unit-testable. Secrets enter as arguments and never leave as anything but
their derived, non-reversible forms.
"""

from jarvis.security.api_keys import (
    generate_api_key,
    hash_api_key,
    is_api_key,
    key_prefix,
)
from jarvis.security.crypto import CryptoError, decrypt, encrypt, key_id, load_master_key
from jarvis.security.passwords import hash_password, verify_password

__all__ = [
    "CryptoError",
    "decrypt",
    "encrypt",
    "generate_api_key",
    "hash_api_key",
    "hash_password",
    "is_api_key",
    "key_id",
    "key_prefix",
    "load_master_key",
    "verify_password",
]
