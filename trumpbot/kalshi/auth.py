"""RSA-PSS signing for Kalshi API requests.

Kalshi requires every authenticated request to carry:

  KALSHI-ACCESS-KEY:       <api_key_id>
  KALSHI-ACCESS-TIMESTAMP: <unix_ms_string>
  KALSHI-ACCESS-SIGNATURE: <base64 RSA-PSS signature of {ts}{method}{path}>

The path used for signing is the URL path *without* the query string
(per Kalshi spec). Signature uses RSA-PSS with SHA-256, MGF1-SHA256,
and salt length equal to the digest length (32 bytes).

The private key is loaded from disk at startup, optionally decrypted
with a passphrase entered manually, held in memory, and never logged
or serialized.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


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
    """Return a base64 RSA-PSS signature over ``{timestamp}{method}{path}``."""
    message = f"{timestamp_ms}{method.upper()}{path}".encode()
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
