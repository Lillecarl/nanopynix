"""Unit tests for pynixd.signing — Ed25519 path signing.

Tests SecretKey parsing, fingerprint construction, and signing roundtrips.
Uses deterministic test keys — no I/O or env dependencies.
"""

from __future__ import annotations

import base64

import nacl.bindings
import pytest

from pynixd.signing import SecretKey, fingerprint, get_default_signing_key, sign_path_info
from pynixd.store_path import StorePath
from pynixd.types import ValidPathInfo
from tests.test_features import TestFeatures as F

_SEED_32 = b"\x00" * 32
_SEED_32_B64 = base64.b64encode(_SEED_32).decode()
_verify_key = nacl.bindings.crypto_sign_seed_keypair(_SEED_32)[0]
_SEED_64 = _SEED_32 + _verify_key
_SEED_64_B64 = base64.b64encode(_SEED_64).decode()


@pytest.mark.covers(F.SIGNING)
class TestSecretKeyParse:
    def test_parse_32_byte_seed(self):
        key = SecretKey._parse(f"test:{_SEED_32_B64}")
        assert key.name == "test"

    def test_parse_64_byte_libsodium(self):
        key = SecretKey._parse(f"test:{_SEED_64_B64}")
        assert key.name == "test"

    def test_parse_from_string(self):
        key = SecretKey.from_string(f"mykey:{_SEED_32_B64}")
        assert key.name == "mykey"

    def test_parse_invalid_length(self):
        with pytest.raises(ValueError, match="Secret key must be 32 or 64 bytes"):
            SecretKey._parse("bad:" + base64.b64encode(b"\x01" * 16).decode())

    def test_parse_empty_key(self):
        with pytest.raises(ValueError):  # noqa: PT011
            SecretKey._parse("empty:")

    def test_public_key_derivation(self):
        key = SecretKey._parse(f"test:{_SEED_32_B64}")
        pub = key.public_key_bytes
        expected_pub = nacl.bindings.crypto_sign_seed_keypair(_SEED_32)[0]
        assert pub == expected_pub

    def test_public_key_string_format(self):
        key = SecretKey._parse(f"test:{_SEED_32_B64}")
        pks = key.public_key_string()
        assert pks.startswith("test:")
        name, b64 = pks.split(":", 1)
        assert name == "test"
        decoded = base64.b64decode(b64)
        assert len(decoded) == 32


class TestSecretKeySign:
    def test_sign_deterministic(self):
        key = SecretKey._parse(f"test:{_SEED_32_B64}")
        data = b"hello world"
        sig1 = key.sign(data)
        sig2 = key.sign(data)
        assert sig1 == sig2
        assert len(sig1) == 64

    def test_sign_fingerprint_format(self):
        key = SecretKey._parse(f"test:{_SEED_32_B64}")
        fp = "1;/nix/store/abc123-foo;sha256:xyz;42;"
        sig_str = key.sign_fingerprint(fp)
        assert sig_str.startswith("test:")
        name, b64 = sig_str.split(":", 1)
        assert name == "test"
        decoded = base64.b64decode(b64)
        assert len(decoded) == 64

    def test_verify_with_public_key(self):
        key = SecretKey._parse(f"test:{_SEED_32_B64}")
        data = b"verify me"
        sig = key.sign(data)
        pub = key.public_key_bytes
        signed_msg = sig + data
        nacl.bindings.crypto_sign_open(signed_msg, pub)


class TestFingerprint:
    def test_basic_fingerprint(self):
        path = StorePath("/nix/store/abc123-foo")
        fp = fingerprint(path, "sha256:xyz", 42, set())
        assert fp == "1;/nix/store/abc123-foo;sha256:xyz;42;"

    def test_with_references_sorted(self):
        path = StorePath("/nix/store/abc123-foo")
        refs = {
            StorePath("/nix/store/zzz-bar"),
            StorePath("/nix/store/aaa-baz"),
        }
        fp = fingerprint(path, "sha256:xyz", 42, refs)
        assert fp == "1;/nix/store/abc123-foo;sha256:xyz;42;/nix/store/aaa-baz,/nix/store/zzz-bar"

    def test_empty_hash(self):
        path = StorePath("/nix/store/abc123-foo")
        fp = fingerprint(path, "", 0, set())
        assert fp == "1;/nix/store/abc123-foo;;0;"

    def test_large_nar_size(self):
        path = StorePath("/nix/store/abc123-foo")
        fp = fingerprint(path, "sha256:xyz", 999999999999, set())
        assert "999999999999" in fp


class TestSignPathInfo:
    def test_sign_path_info_roundtrip(self):

        key = SecretKey._parse(f"test:{_SEED_32_B64}")
        path = StorePath("/nix/store/abc123-foo")
        info = ValidPathInfo(
            path=path,
            nar_hash="sha256:xyz",
            nar_size=42,
            references={StorePath("/nix/store/ref1-dep")},
        )

        sig = sign_path_info(key, info)
        assert sig.startswith("test:")

        fp = fingerprint(info.path, info.nar_hash, info.nar_size, info.references)
        sig_bytes = sig.split(":", 1)[1]
        signed_msg = base64.b64decode(sig_bytes) + fp.encode("utf-8")
        nacl.bindings.crypto_sign_open(signed_msg, key.public_key_bytes)


class TestGetDefaultSigningKey:
    def test_no_env_var(self, monkeypatch):
        monkeypatch.delenv("PYNIXD_SIGNING_KEY", raising=False)

        assert get_default_signing_key() is None

    def test_with_env_var(self, monkeypatch):
        key_str = f"mykey:{_SEED_32_B64}"
        monkeypatch.setenv("PYNIXD_SIGNING_KEY", key_str)

        key = get_default_signing_key()
        assert key is not None
        assert key.name == "mykey"
