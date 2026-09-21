"""Shared test fixtures.

Key generation (RSA-3072 especially) is expensive, so keypairs are created
once per test session and reused. This also mirrors production behaviour:
keys are generated once and persisted, never regenerated per operation.
"""

from __future__ import annotations

from typing import NamedTuple

import pytest

from shared.crypto import (
    generate_rsa_keypair,
    generate_signing_keypair,
    load_rsa_private_key,
    load_rsa_public_key,
    load_signing_private_key,
    load_signing_public_key,
)


class KeyBundle(NamedTuple):
    """Every key the tests need, plus the PEM text they were loaded from."""

    signing_private_pem: str
    signing_public_pem: str
    rsa_private_pem: str
    rsa_public_pem: str

    @property
    def signing_private(self):  # type: ignore[no-untyped-def]
        return load_signing_private_key(self.signing_private_pem)

    @property
    def signing_public(self):  # type: ignore[no-untyped-def]
        return load_signing_public_key(self.signing_public_pem)

    @property
    def rsa_private(self):  # type: ignore[no-untyped-def]
        return load_rsa_private_key(self.rsa_private_pem)

    @property
    def rsa_public(self):  # type: ignore[no-untyped-def]
        return load_rsa_public_key(self.rsa_public_pem)


@pytest.fixture(scope="session")
def keys() -> KeyBundle:
    """A primary keypair set, generated once for the whole session."""
    signing_private_pem, signing_public_pem = generate_signing_keypair()
    rsa_private_pem, rsa_public_pem = generate_rsa_keypair()
    return KeyBundle(
        signing_private_pem=signing_private_pem,
        signing_public_pem=signing_public_pem,
        rsa_private_pem=rsa_private_pem,
        rsa_public_pem=rsa_public_pem,
    )


@pytest.fixture(scope="session")
def other_keys() -> KeyBundle:
    """A second, unrelated keypair set, for cross-key rejection tests."""
    signing_private_pem, signing_public_pem = generate_signing_keypair()
    rsa_private_pem, rsa_public_pem = generate_rsa_keypair()
    return KeyBundle(
        signing_private_pem=signing_private_pem,
        signing_public_pem=signing_public_pem,
        rsa_private_pem=rsa_private_pem,
        rsa_public_pem=rsa_public_pem,
    )
