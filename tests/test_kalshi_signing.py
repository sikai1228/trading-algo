"""Pinned-vector tests for Kalshi REST + WS signing.

These tests lock in the verified-working configuration from the manual
test against
``https://api.elections.kalshi.com/trade-api/v2/portfolio/balance`` on
2026-04-25. The most common authentication failure mode is the REST
signing path losing its ``/trade-api/v2`` prefix; the regression test
``test_signature_includes_path_prefix`` fails loudly if that ever
happens.

We deliberately do not assert byte-equality of the signature itself —
RSA-PSS uses a randomized salt, so signatures over identical inputs
differ across calls. We assert (a) the *signing message string* is
exactly what Kalshi expects, and (b) the resulting signature is
base64-decodable and verifies against the public key.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from trumpbot.kalshi.auth import (
    API_PATH_PREFIX,
    WS_AUTH_PATH,
    sign_request,
    signed_resource_path,
    signing_message,
)

# Pinned input vector — do not change without re-verifying against
# Kalshi production.
PINNED_TIMESTAMP_MS = "1777151610000"
PINNED_METHOD = "GET"
PINNED_RESOURCE = "/portfolio/balance"
PINNED_EXPECTED_MESSAGE = "1777151610000GET/trade-api/v2/portfolio/balance"


class TestPathPrefixConstants:
    def test_api_path_prefix_value(self) -> None:
        assert API_PATH_PREFIX == "/trade-api/v2"

    def test_ws_auth_path_value(self) -> None:
        assert WS_AUTH_PATH == "/trade-api/ws/v2"

    def test_signed_resource_path_prepends_prefix(self) -> None:
        assert signed_resource_path("/portfolio/balance") == "/trade-api/v2/portfolio/balance"

    def test_signed_resource_path_requires_leading_slash(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            signed_resource_path("portfolio/balance")


class TestSigningMessage:
    def test_pinned_signing_message(self) -> None:
        """The exact byte-string Kalshi must see for the pinned input."""
        msg = signing_message(
            PINNED_TIMESTAMP_MS, PINNED_METHOD, signed_resource_path(PINNED_RESOURCE)
        )
        assert msg == PINNED_EXPECTED_MESSAGE

    def test_signing_message_uppercases_method(self) -> None:
        msg = signing_message("123", "get", "/x")
        assert msg == "123GET/x"

    def test_signing_message_does_not_alter_path(self) -> None:
        # path is passed through verbatim; case + slashes preserved.
        msg = signing_message("999", "POST", "/Trade-API/MIXED")
        assert msg == "999POST/Trade-API/MIXED"


class TestSignatureIncludesPathPrefix:
    """Regression: REST signing MUST include the /trade-api/v2 prefix.

    If this test fails, every authenticated REST call to Kalshi will
    return 401. This was verified empirically on 2026-04-25 — the
    signing path used by the REST client is exactly the value asserted
    here.
    """

    def test_signature_includes_path_prefix(self) -> None:
        # The check is on the signing-message construction, not the
        # signature bytes (which vary per call). If anyone changes
        # API_PATH_PREFIX or signed_resource_path, this assertion
        # changes shape and the test breaks loudly.
        signed_path = signed_resource_path("/markets")
        msg = signing_message("1700000000000", "GET", signed_path)
        assert "/trade-api/v2/markets" in msg
        assert msg.startswith("1700000000000GET/trade-api/v2/")

    def test_rest_client_uses_signed_resource_path(self) -> None:
        """Source-level guard: ``client.py`` must call ``signed_resource_path``
        rather than inline the prefix."""
        from pathlib import Path

        client_src = Path(__file__).resolve().parent.parent / "trumpbot/kalshi/client.py"
        text = client_src.read_text()
        assert "signed_resource_path(path)" in text, (
            "trumpbot/kalshi/client.py must use signed_resource_path() to "
            "build the signing path. Hardcoding the /trade-api/v2 prefix "
            "loses the single-source-of-truth guarantee."
        )
        # And no inlined literal of the prefix in client.py — except in
        # the documentation/comment block.
        non_comment_lines = [
            line
            for line in text.splitlines()
            if not line.lstrip().startswith("#") and "/trade-api/v2" in line
        ]
        # The DEFAULT_BASE_URL line builds the URL from API_PATH_PREFIX,
        # so the literal "/trade-api/v2" should not appear in code lines.
        assert (
            non_comment_lines == []
        ), f"unexpected /trade-api/v2 literals in client.py code: {non_comment_lines}"

    def test_ws_feed_uses_ws_auth_path_constant(self) -> None:
        from pathlib import Path

        ws_src = Path(__file__).resolve().parent.parent / "trumpbot/market_data/kalshi_ws.py"
        text = ws_src.read_text()
        assert "WS_AUTH_PATH" in text
        # Same guard for the WS file.
        non_comment_lines = [
            line
            for line in text.splitlines()
            if not line.lstrip().startswith("#") and "/trade-api/" in line
        ]
        assert (
            non_comment_lines == []
        ), f"unexpected /trade-api/ literals in kalshi_ws.py: {non_comment_lines}"


class TestSignatureRoundTrip:
    """The pinned input produces a base64-valid PSS signature that
    verifies against the public key. We cannot assert byte-equality
    because PSS uses a random salt; cryptographic correctness is the
    substantive check."""

    def test_signature_is_base64_decodable(self, rsa_private_key: RSAPrivateKey) -> None:
        signed_path = signed_resource_path(PINNED_RESOURCE)
        sig_b64 = sign_request(
            rsa_private_key,
            timestamp_ms=PINNED_TIMESTAMP_MS,
            method=PINNED_METHOD,
            path=signed_path,
        )
        # base64.b64decode raises if the string is not valid base64.
        sig = base64.b64decode(sig_b64, validate=True)
        # 2048-bit RSA produces 256-byte signatures.
        assert len(sig) == 256

    def test_signature_verifies_against_public_key(self, rsa_private_key: RSAPrivateKey) -> None:
        signed_path = signed_resource_path(PINNED_RESOURCE)
        sig_b64 = sign_request(
            rsa_private_key,
            timestamp_ms=PINNED_TIMESTAMP_MS,
            method=PINNED_METHOD,
            path=signed_path,
        )
        rsa_private_key.public_key().verify(
            base64.b64decode(sig_b64),
            PINNED_EXPECTED_MESSAGE.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256.digest_size,
            ),
            hashes.SHA256(),
        )

    def test_signature_with_wrong_prefix_fails_to_verify(
        self, rsa_private_key: RSAPrivateKey
    ) -> None:
        """Sanity check: signing the resource path WITHOUT the prefix
        produces a signature that does NOT verify against the
        prefix-included message. This is the exact bug class the
        prefix constant guards against."""
        bad_sig_b64 = sign_request(
            rsa_private_key,
            timestamp_ms=PINNED_TIMESTAMP_MS,
            method=PINNED_METHOD,
            path=PINNED_RESOURCE,  # missing the prefix
        )
        import pytest
        from cryptography.exceptions import InvalidSignature

        with pytest.raises(InvalidSignature):
            rsa_private_key.public_key().verify(
                base64.b64decode(bad_sig_b64),
                PINNED_EXPECTED_MESSAGE.encode(),
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256.digest_size,
                ),
                hashes.SHA256(),
            )
