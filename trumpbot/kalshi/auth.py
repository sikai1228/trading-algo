"""RSA-PSS signing for Kalshi API requests.

Kalshi requires every authenticated request to carry:

  KALSHI-ACCESS-KEY:       <api_key_id>
  KALSHI-ACCESS-TIMESTAMP: <unix_ms_string>
  KALSHI-ACCESS-SIGNATURE: <base64 RSA-PSS signature of {ts}{method}{path}>

The path used for signing is the URL path *without* the query string,
**including** the ``/trade-api/v2`` prefix for REST. Signing only the
resource path (e.g. ``/portfolio/balance`` instead of
``/trade-api/v2/portfolio/balance``) produces a 401 from Kalshi —
this is the most common authentication bug, so the prefix is held in
exactly one place (``API_PATH_PREFIX``) and reached only through
``signed_resource_path``.

Signature uses RSA-PSS with SHA-256, MGF1-SHA256, and salt length
equal to the digest length (32 bytes).

The private key is loaded from disk at startup, optionally decrypted
with a passphrase entered manually, held in memory, and never logged
or serialized.

Verified 2026-04-25 against
https://api.elections.kalshi.com/trade-api/v2/portfolio/balance — the
configuration in this module returned a valid balance response.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

# ---------------------------------------------------------------------------
# Single source of truth for Kalshi path prefixes used in signing.
#
# Changing either of these breaks authentication for every request from
# the moment of deployment. Every REST call site goes through
# ``signed_resource_path``; the WebSocket auth uses ``WS_AUTH_PATH``
# directly.  Do not duplicate the literal strings anywhere else in the
# codebase — a regression test
# (``tests/test_kalshi_signing.test_signature_includes_path_prefix``)
# fails loudly if the REST prefix is ever dropped.
# ---------------------------------------------------------------------------

API_PATH_PREFIX: Final[str] = "/trade-api/v2"
"""REST path prefix that MUST be included in the signing message."""

WS_AUTH_PATH: Final[str] = "/trade-api/ws/v2"
"""Path Kalshi expects in the WS connect-time signature."""


def signed_resource_path(resource_path: str) -> str:
    """Return the path Kalshi expects in the signing message for a REST resource.

    Example::

        >>> signed_resource_path("/portfolio/balance")
        '/trade-api/v2/portfolio/balance'

    **Critical:** this function is the only sanctioned way to construct
    a REST signing path. Inlining the prefix elsewhere recreates the
    bug class this helper exists to prevent (signature mismatches due
    to prefix drift between code paths).
    """
    if not resource_path.startswith("/"):
        raise ValueError(f"resource_path must begin with '/'; got {resource_path!r}")
    return f"{API_PATH_PREFIX}{resource_path}"


def signing_message(timestamp_ms: str, method: str, path: str) -> str:
    """Construct the exact byte-string Kalshi signs.

    The format is ``{timestamp_ms}{METHOD_UPPERCASE}{path}``. Pass the
    path as Kalshi expects it on the wire (i.e. via
    :func:`signed_resource_path` for REST, or :data:`WS_AUTH_PATH` for
    the WebSocket handshake).
    """
    return f"{timestamp_ms}{method.upper()}{path}"


def load_private_key(path: Path | str, passphrase: bytes | None = None) -> RSAPrivateKey:
    """Load a PEM-encoded RSA private key from disk.

    The on-disk file should be mode 0600 and owned by the service user.
    If the key is encrypted, ``passphrase`` is required.
    """
    pem_bytes = Path(path).read_bytes()
    key = serialization.load_pem_private_key(pem_bytes, password=passphrase)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError("Kalshi private key must be RSA")
    return key


def sign_request(
    private_key: RSAPrivateKey,
    *,
    timestamp_ms: str,
    method: str,
    path: str,
) -> str:
    """Return a base64 RSA-PSS signature over ``{timestamp}{method}{path}``.

    The ``path`` argument must already include the ``/trade-api/v2``
    prefix for REST requests (use :func:`signed_resource_path`) or be
    :data:`WS_AUTH_PATH` for the WebSocket handshake. Passing only the
    resource portion produces a signature Kalshi rejects with 401.
    """
    message = signing_message(timestamp_ms, method, path).encode()
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256.digest_size,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def now_timestamp_ms() -> str:
    """Current Unix time in milliseconds, as a decimal string."""
    return str(int(time.time() * 1000))


@dataclass(frozen=True)
class KalshiAuthHeaders:
    """The three headers Kalshi expects on every authenticated request."""

    access_key: str
    timestamp_ms: str
    signature: str

    def as_dict(self) -> dict[str, str]:
        return {
            "KALSHI-ACCESS-KEY": self.access_key,
            "KALSHI-ACCESS-TIMESTAMP": self.timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self.signature,
        }


def build_auth_headers(
    *,
    api_key_id: str,
    private_key: RSAPrivateKey,
    method: str,
    path: str,
    timestamp_ms: str | None = None,
) -> KalshiAuthHeaders:
    """Assemble the three Kalshi auth headers for a single request."""
    ts = timestamp_ms or now_timestamp_ms()
    sig = sign_request(private_key, timestamp_ms=ts, method=method, path=path)
    return KalshiAuthHeaders(access_key=api_key_id, timestamp_ms=ts, signature=sig)
