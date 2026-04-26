"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, generate_private_key

from trumpbot.db.connection import Database
from trumpbot.discovery.subjects import DEFAULT_SUBJECT_ALIASES, SubjectExtractor


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "test.db")
    db.connect()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="session")
def rsa_private_key() -> RSAPrivateKey:
    return generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_private_key_pem(rsa_private_key: RSAPrivateKey) -> bytes:
    return rsa_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture()
def extractor() -> SubjectExtractor:
    return SubjectExtractor(aliases=DEFAULT_SUBJECT_ALIASES)


# typing helper used by tests that pass rsa_private_key into a fixture
RSAKeyFixture = Any
