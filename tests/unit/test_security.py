"""Security primitives (S2): scrypt passwords, API-key hashing, AES-GCM
credential envelope — roundtrip, tamper detection, key mismatch."""

from __future__ import annotations

import base64
import os

import pytest

from jarvis.config import Settings
from jarvis.security import (
    CryptoError,
    decrypt,
    encrypt,
    generate_api_key,
    hash_api_key,
    hash_password,
    is_api_key,
    key_id,
    key_prefix,
    load_master_key,
    verify_password,
)


def _master_key() -> bytes:
    return os.urandom(32)


class TestPasswords:
    def test_roundtrip(self):
        stored = hash_password("correct horse battery staple")
        assert stored.startswith("scrypt$")
        assert verify_password("correct horse battery staple", stored)

    def test_wrong_password_rejected(self):
        stored = hash_password("hunter2")
        assert not verify_password("hunter3", stored)

    def test_salts_are_unique(self):
        assert hash_password("same") != hash_password("same")

    def test_malformed_hash_rejected(self):
        assert not verify_password("x", "not-a-hash")
        assert not verify_password("x", "argon2$id$salt$hash")
        assert not verify_password("x", "scrypt$bad$bad$bad$!!!$!!!")

    def test_wrong_master_password_parameters_rejected(self):
        # A hash with a truncated digest must not verify.
        stored = hash_password("x")
        assert not verify_password("x", stored[:-4] + "AAAA")


class TestApiKeys:
    def test_shape_and_prefix(self):
        key = generate_api_key()
        assert key.startswith("jarvis_sk_")
        assert len(key) == len("jarvis_sk_") + 64
        assert key_prefix(key) == key[:12]

    def test_keys_are_unique_and_recognized(self):
        keys = {generate_api_key() for _ in range(50)}
        assert len(keys) == 50
        assert is_api_key(generate_api_key())
        assert not is_api_key("sk-proj-whatever")

    def test_hash_is_deterministic_and_not_reversible_shape(self):
        key = generate_api_key()
        assert hash_api_key(key) == hash_api_key(key)
        assert len(hash_api_key(key)) == 64
        assert key not in hash_api_key(key)


class TestCredentialCrypto:
    def test_roundtrip(self):
        key = _master_key()
        envelope = encrypt("sk-live-abc123", master_key=key)
        assert set(envelope) == {"v", "key_id", "nonce", "ct"}
        assert "sk-live" not in str(envelope)
        assert decrypt(envelope, master_key=key) == "sk-live-abc123"

    def test_nonces_unique_per_call(self):
        key = _master_key()
        assert encrypt("s", master_key=key)["nonce"] != encrypt("s", master_key=key)["nonce"]

    def test_tampered_ciphertext_fails(self):
        key = _master_key()
        envelope = encrypt("secret", master_key=key)
        ct = base64.urlsafe_b64decode(envelope["ct"] + "==")
        envelope["ct"] = (
            base64.urlsafe_b64encode(bytes([ct[0] ^ 0xFF]) + ct[1:]).decode().rstrip("=")
        )
        with pytest.raises(CryptoError):
            decrypt(envelope, master_key=key)

    def test_wrong_key_fails(self):
        envelope = encrypt("secret", master_key=_master_key())
        with pytest.raises(CryptoError):
            decrypt(envelope, master_key=_master_key())

    def test_malformed_envelope_fails(self):
        with pytest.raises(CryptoError):
            decrypt({}, master_key=_master_key())
        with pytest.raises(CryptoError):
            decrypt({"v": "99", "key_id": "x", "nonce": "", "ct": ""}, master_key=_master_key())

    def test_key_id_is_stable_short_fingerprint(self):
        key = _master_key()
        assert key_id(key) == key_id(key)
        assert len(key_id(key)) == 16


class TestLoadMasterKey:
    def _settings(self) -> Settings:
        return Settings(credentials_master_key_env="TEST_MASTER_KEY")

    def test_missing_env_raises(self, monkeypatch):
        monkeypatch.delenv("TEST_MASTER_KEY", raising=False)
        with pytest.raises(CryptoError):
            load_master_key(self._settings())

    def test_roundtrip_through_env(self, monkeypatch):
        key = _master_key()
        monkeypatch.setenv("TEST_MASTER_KEY", base64.urlsafe_b64encode(key).decode().rstrip("="))
        loaded = load_master_key(self._settings())
        assert loaded == key
        envelope = encrypt("s", master_key=loaded)
        assert decrypt(envelope, master_key=loaded) == "s"

    def test_wrong_length_raises(self, monkeypatch):
        monkeypatch.setenv("TEST_MASTER_KEY", base64.urlsafe_b64encode(b"short").decode())
        with pytest.raises(CryptoError):
            load_master_key(self._settings())

    def test_no_env_var_name_configured(self):
        settings = Settings(credentials_master_key_env="")
        with pytest.raises(CryptoError):
            load_master_key(settings)
