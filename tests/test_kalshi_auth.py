"""Tests for trumpbot.kalshi.auth (RSA-PSS signing)."""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from trumpbot.kalshi.auth import (
    build_auth_headers,
    load_private_key,
    now_timestamp_ms,
    sign_request,
)


class TestSign:
    def test_signature_is_valid(self, rsa_private_key: RSAPrivateKey) -> None:
        ts = "1700000000000"
        sig_b64 = sign_request(rsa_private_key, timestamp_ms=ts, method="GET", path="/foo")
        sig = base64.b64decode(sig_b64)
        msg = f"{ts}GET/foo".encode()
        rsa_private_key.public_key().verify(
            sig,
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256.digest_size,
            ),
            hashes.SHA256(),
        )

    def test_method_uppercased(self, rsa_private_key: RSAPrivateKey) -> None:
        ts = "1700000000000"
        a = sign_request(rsa_private_key, timestamp_ms=ts, method="get", path="/foo")
        b = sign_request(rsa_private_key, timestamp_ms=ts, method="GET", path="/foo")
        for sig_b64 in (a, b):
            rsa_private_key.public_key().verify(
                base64.b64decode(sig_b64),
                f"{ts}GET/foo".encode(),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256.digest_size,
                ),
                hashes.SHA256(),
            )

    def test_build_auth_headers_contains_required_keys(
        self, rsa_private_key: RSAPrivateKey
    ) -> None:
        h = build_auth_headers(
            api_key_id="key-1",
            private_key=rsa_private_key,
            method="GET",
            path="/trade-api/v2/markets",
            timestamp_ms="1700000000000",
        )
        d = h.as_dict()
        assert d["KALSHI-ACCESS-KEY"] == "key-1"
        assert d["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
        assert d["KALSHI-ACCESS-SIGNATURE"]

    def test_now_timestamp_ms_is_decimal_string(self) -> None:
        s = now_timestamp_ms()
        assert s.isdigit()
        assert len(s) >= 13


class TestLoad:
    def test_load_unencrypted_pem(self, tmp_path: Path, rsa_private_key_pem: bytes) -> None:
        p = tmp_path / "key.pem"
        p.write_bytes(rsa_private_key_pem)
        key = load_private_key(p)
        assert key.key_size == 2048

    def test_load_encrypted_pem(self, tmp_path: Path, rsa_private_key: RSAPrivateKey) -> None:
        passphrase = b"test-passphrase"
        pem = rsa_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(passphrase),
        )
        p = tmp_path / "key.pem"
        p.write_bytes(pem)
        key = load_private_key(p, passphrase=passphrase)
        assert key.key_size == 2048
