"""Cryptographic core for MeshSettle.

This module owns every cryptographic decision in the system. Nothing else
signs, verifies, encrypts, or decrypts. It deals in primitives (bytes,
str, dict) and knows nothing about Pydantic models or FastAPI, so that the
packet schema in `shared/models.py` can be built on top of it without a
circular import.

Scheme (see ARCHITECTURE.md "Packet Format"):

- Payload confidentiality: AES-256-GCM with a fresh 96-bit nonce and a
  fresh 256-bit content key per packet.
- Content key transport: the AES key is RSA-OAEP wrapped to the settlement
  service's public key, so only settlement can read the instruction. Relay
  nodes carry ciphertext they cannot open.
- Integrity/authenticity: Ed25519 signature over the canonical serialization
  of the cleartext header plus the whole envelope.
- Replay binding: the GCM AAD binds the ciphertext to `packet_id` and
  `sender_id`, so a validly-encrypted payload cannot be re-headered.

Failures raise. Nothing here returns a bare ``False`` that a caller might
forget to check (see RULES.md: never fail silently).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from functools import lru_cache
from typing import Any, NamedTuple

from cryptography.exceptions import InvalidSignature as _InvalidSignature
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from shared.config import normalize_pem, settings

# --- Scheme parameters -------------------------------------------------------

AES_KEY_BITS = 256
AES_KEY_BYTES = AES_KEY_BITS // 8
GCM_NONCE_BYTES = 12  # 96-bit nonce, the size AES-GCM is specified for
RSA_KEY_BITS = 3072  # >= 2048 required; 3072 for a comfortable margin
RSA_PUBLIC_EXPONENT = 65537
IDEMPOTENCY_KEY_PREFIX = "settle:"


# --- Errors ------------------------------------------------------------------


class CryptoError(Exception):
    """Base class for every cryptographic failure in MeshSettle."""


class KeyConfigurationError(CryptoError):
    """A required key was missing, empty, or not parseable as the right type."""


class SignatureVerificationError(CryptoError):
    """The packet signature did not verify. The packet must be rejected."""


class DecryptionError(CryptoError):
    """AES-GCM or RSA-OAEP decryption failed (bad key, bad tag, or tamper)."""


# --- Encoding helpers --------------------------------------------------------


def b64u_encode(raw: bytes) -> str:
    """base64url encode without padding, for JSON-safe byte transport."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_decode(encoded: str) -> bytes:
    """Inverse of :func:`b64u_encode`, restoring stripped padding.

    Raises:
        ValueError: if the input is not valid base64url.
    """
    padding_needed = (-len(encoded)) % 4
    try:
        return base64.urlsafe_b64decode(encoded + ("=" * padding_needed))
    except Exception as exc:  # noqa: BLE001 - normalize to ValueError for callers
        raise ValueError("value is not valid base64url") from exc


def canonical_json(payload: dict[str, Any]) -> bytes:
    """Serialize a mapping deterministically.

    Sorted keys, no insignificant whitespace, UTF-8. Two structurally equal
    mappings always produce byte-identical output, which is what makes the
    signature and the idempotency key reproducible on the settlement side
    after a JSON round trip through the queue.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


# --- Key generation ----------------------------------------------------------


def generate_signing_keypair() -> tuple[str, str]:
    """Generate an Ed25519 signing keypair.

    Returns:
        ``(private_pem, public_pem)`` as PEM text. Generated once and then
        persisted via environment/secret injection; never regenerated on
        service boot (RULES.md).
    """
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        # The PEM itself is the secret and lives in a secret store / env var.
        # Adding a passphrase here would just move the secret, not remove it.
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


def generate_rsa_keypair(key_bits: int = RSA_KEY_BITS) -> tuple[str, str]:
    """Generate an RSA keypair for AES content-key wrapping.

    Returns:
        ``(private_pem, public_pem)`` as PEM text.
    """
    private_key = rsa.generate_private_key(
        public_exponent=RSA_PUBLIC_EXPONENT,
        key_size=key_bits,
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_pem, public_pem


# --- Key loading -------------------------------------------------------------


def load_signing_private_key(pem: str) -> Ed25519PrivateKey:
    """Parse an Ed25519 private key from PEM text."""
    if not pem or not pem.strip():
        raise KeyConfigurationError("signing private key PEM is empty")
    try:
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    except Exception as exc:  # noqa: BLE001 - never surface key bytes in the message
        raise KeyConfigurationError("signing private key PEM could not be parsed") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyConfigurationError(f"expected an Ed25519 private key, got {type(key).__name__}")
    return key


def load_signing_public_key(pem: str) -> Ed25519PublicKey:
    """Parse an Ed25519 public key from PEM text."""
    if not pem or not pem.strip():
        raise KeyConfigurationError("signing public key PEM is empty")
    try:
        key = serialization.load_pem_public_key(pem.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise KeyConfigurationError("signing public key PEM could not be parsed") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise KeyConfigurationError(f"expected an Ed25519 public key, got {type(key).__name__}")
    return key


def load_rsa_private_key(pem: str) -> rsa.RSAPrivateKey:
    """Parse an RSA private key from PEM text."""
    if not pem or not pem.strip():
        raise KeyConfigurationError("RSA private key PEM is empty")
    try:
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    except Exception as exc:  # noqa: BLE001
        raise KeyConfigurationError("RSA private key PEM could not be parsed") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise KeyConfigurationError(f"expected an RSA private key, got {type(key).__name__}")
    return key


def load_rsa_public_key(pem: str) -> rsa.RSAPublicKey:
    """Parse an RSA public key from PEM text."""
    if not pem or not pem.strip():
        raise KeyConfigurationError("RSA public key PEM is empty")
    try:
        key = serialization.load_pem_public_key(pem.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise KeyConfigurationError("RSA public key PEM could not be parsed") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise KeyConfigurationError(f"expected an RSA public key, got {type(key).__name__}")
    return key


# --- Key loading from configuration (cached; keys persist across restarts) ---


@lru_cache(maxsize=1)
def sender_signing_private_key() -> Ed25519PrivateKey:
    """The sender device's Ed25519 signing key, from the environment."""
    return load_signing_private_key(normalize_pem(settings.sender_signing_private_key_pem))


@lru_cache(maxsize=1)
def sender_signing_public_key() -> Ed25519PublicKey:
    """The sender's Ed25519 verification key, from the environment."""
    return load_signing_public_key(normalize_pem(settings.sender_signing_public_key_pem))


@lru_cache(maxsize=1)
def settlement_rsa_private_key() -> rsa.RSAPrivateKey:
    """Settlement's RSA unwrapping key. Only the settlement service has this."""
    return load_rsa_private_key(normalize_pem(settings.settlement_rsa_private_key_pem))


@lru_cache(maxsize=1)
def settlement_rsa_public_key() -> rsa.RSAPublicKey:
    """Settlement's RSA public key, used by senders to wrap the content key."""
    return load_rsa_public_key(normalize_pem(settings.settlement_rsa_public_key_pem))


def reset_key_cache() -> None:
    """Drop cached keys. Used by tests that swap key material at runtime."""
    sender_signing_private_key.cache_clear()
    sender_signing_public_key.cache_clear()
    settlement_rsa_private_key.cache_clear()
    settlement_rsa_public_key.cache_clear()


# --- AAD and signing-byte construction --------------------------------------


def build_aad(*, packet_id: str, sender_id: str) -> bytes:
    """Build the GCM additional authenticated data.

    Binding the ciphertext to ``packet_id`` and ``sender_id`` means an
    attacker cannot take a validly encrypted payload and present it under a
    different packet id to dodge deduplication: the GCM tag check fails.
    """
    return canonical_json({"packet_id": packet_id, "sender_id": sender_id})


def build_signing_bytes(
    *,
    packet_id: str,
    sender_id: str,
    created_at: str,
    encrypted_key: bytes,
    nonce: bytes,
    ciphertext: bytes,
    aad: bytes,
) -> bytes:
    """Build the canonical byte string that the Ed25519 signature covers.

    Everything a settlement decision depends on is inside this region. The
    packet's ``hops`` list is deliberately excluded, because relay nodes
    append to it in flight and any signature over it would break at the
    first hop (see ARCHITECTURE.md). Hops are untrusted metadata.
    """
    return canonical_json(
        {
            "packet_id": packet_id,
            "sender_id": sender_id,
            "created_at": created_at,
            "encrypted_key": b64u_encode(encrypted_key),
            "nonce": b64u_encode(nonce),
            "ciphertext": b64u_encode(ciphertext),
            "aad": b64u_encode(aad),
        }
    )


def derive_idempotency_key(signing_bytes: bytes) -> str:
    """Derive the Redis dedupe key from the signed region.

    Because ``hops`` is outside the signed region, the same instruction
    arriving by two different mesh paths yields the same key, which is
    exactly the deduplication we want.
    """
    digest = hashlib.sha256(signing_bytes).hexdigest()
    return f"{IDEMPOTENCY_KEY_PREFIX}{digest}"


# --- Hybrid encryption -------------------------------------------------------


class EncryptedPayload(NamedTuple):
    """Raw output of :func:`encrypt_payload`, before it becomes a Pydantic model."""

    encrypted_key: bytes
    nonce: bytes
    ciphertext: bytes
    aad: bytes


def encrypt_payload(
    plaintext: bytes,
    recipient_public_key: rsa.RSAPublicKey,
    aad: bytes,
) -> EncryptedPayload:
    """Encrypt a payload with a fresh AES-256-GCM key, RSA-OAEP wrapping the key.

    A fresh content key and nonce are generated per call, so nonce reuse
    across packets is structurally impossible.
    """
    content_key = AESGCM.generate_key(bit_length=AES_KEY_BITS)
    nonce = _random_nonce()
    ciphertext = AESGCM(content_key).encrypt(nonce, plaintext, aad)
    encrypted_key = recipient_public_key.encrypt(
        content_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return EncryptedPayload(
        encrypted_key=encrypted_key,
        nonce=nonce,
        ciphertext=ciphertext,
        aad=aad,
    )


def decrypt_payload(
    *,
    encrypted_key: bytes,
    nonce: bytes,
    ciphertext: bytes,
    aad: bytes,
    recipient_private_key: rsa.RSAPrivateKey,
) -> bytes:
    """Unwrap the AES key with RSA-OAEP, then open the AES-256-GCM ciphertext.

    Raises:
        DecryptionError: if the key cannot be unwrapped, the GCM tag does not
            match, or the AAD differs from the one used at encryption time.
            The message never includes key or plaintext material.
    """
    try:
        content_key = recipient_private_key.decrypt(
            encrypted_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        raise DecryptionError("content key could not be unwrapped") from exc

    if len(content_key) != AES_KEY_BYTES:
        raise DecryptionError("unwrapped content key has the wrong length")

    try:
        return AESGCM(content_key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise DecryptionError("AES-GCM authentication tag mismatch") from exc
    except Exception as exc:  # noqa: BLE001
        raise DecryptionError("payload could not be decrypted") from exc


def _random_nonce() -> bytes:
    """A fresh 96-bit GCM nonce from the OS CSPRNG."""
    return os.urandom(GCM_NONCE_BYTES)


# --- Signing and verification ------------------------------------------------


def sign_bytes(signing_bytes: bytes, private_key: Ed25519PrivateKey) -> bytes:
    """Produce a 64-byte Ed25519 signature over ``signing_bytes``."""
    return private_key.sign(signing_bytes)


def verify_signature(
    signing_bytes: bytes,
    signature: bytes,
    public_key: Ed25519PublicKey,
) -> None:
    """Verify an Ed25519 signature, raising on any failure.

    Returns ``None`` on success and raises otherwise, so a caller cannot
    accidentally treat a falsy return as "verified".

    Raises:
        SignatureVerificationError: if the signature does not match.
    """
    try:
        public_key.verify(signature, signing_bytes)
    except _InvalidSignature as exc:
        raise SignatureVerificationError("packet signature did not verify") from exc
    except Exception as exc:  # noqa: BLE001
        raise SignatureVerificationError("packet signature could not be checked") from exc
