"""Unit tests for the crypto core.

The three tests TASK.md Phase 1 requires are:
  * a valid packet verifies correctly
  * a tampered payload is rejected
  * a tampered signature is rejected

Everything else here defends a property the settlement service depends on:
canonical serialization determinism, AAD replay binding, idempotency key
stability across hops, and key persistence across "restarts".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from shared.crypto import (
    AES_KEY_BYTES,
    GCM_NONCE_BYTES,
    DecryptionError,
    KeyConfigurationError,
    SignatureVerificationError,
    b64u_decode,
    b64u_encode,
    build_aad,
    build_signing_bytes,
    canonical_json,
    decrypt_payload,
    derive_idempotency_key,
    encrypt_payload,
    generate_rsa_keypair,
    generate_signing_keypair,
    load_rsa_private_key,
    load_rsa_public_key,
    load_signing_private_key,
    load_signing_public_key,
    sign_bytes,
    verify_signature,
)

# --- Helpers -----------------------------------------------------------------


def _instruction(amount_minor: int = 125_00) -> dict[str, object]:
    """A representative payment instruction plaintext."""
    return {
        "packet_id": str(uuid4()),
        "payer_id": "device-alice",
        "payee_id": "device-bob",
        "amount_minor": amount_minor,
        "currency": "INR",
        "created_at": datetime.now(UTC).isoformat(),
    }


def _build_packet(keys, instruction: dict[str, object] | None = None) -> dict[str, object]:
    """Create a complete signed+encrypted packet as the sender would.

    Returns a plain dict so these tests stay independent of the Pydantic
    models introduced in Phase 2.
    """
    instruction = instruction or _instruction()
    packet_id = str(instruction["packet_id"])
    sender_id = "device-alice"
    created_at = datetime.now(UTC).isoformat()

    aad = build_aad(packet_id=packet_id, sender_id=sender_id)
    payload = encrypt_payload(canonical_json(instruction), keys.rsa_public, aad)

    signing_bytes = build_signing_bytes(
        packet_id=packet_id,
        sender_id=sender_id,
        created_at=created_at,
        encrypted_key=payload.encrypted_key,
        nonce=payload.nonce,
        ciphertext=payload.ciphertext,
        aad=payload.aad,
    )
    signature = sign_bytes(signing_bytes, keys.signing_private)

    return {
        "packet_id": packet_id,
        "sender_id": sender_id,
        "created_at": created_at,
        "encrypted_key": payload.encrypted_key,
        "nonce": payload.nonce,
        "ciphertext": payload.ciphertext,
        "aad": payload.aad,
        "signature": signature,
        "instruction": instruction,
    }


def _signing_bytes_of(packet: dict[str, object]) -> bytes:
    return build_signing_bytes(
        packet_id=str(packet["packet_id"]),
        sender_id=str(packet["sender_id"]),
        created_at=str(packet["created_at"]),
        encrypted_key=packet["encrypted_key"],  # type: ignore[arg-type]
        nonce=packet["nonce"],  # type: ignore[arg-type]
        ciphertext=packet["ciphertext"],  # type: ignore[arg-type]
        aad=packet["aad"],  # type: ignore[arg-type]
    )


def _flip_last_byte(raw: bytes) -> bytes:
    """Flip one bit in the final byte, the smallest possible tamper."""
    return raw[:-1] + bytes([raw[-1] ^ 0x01])


# --- Required test 1: a valid packet verifies and decrypts -------------------


def test_valid_packet_verifies_and_decrypts(keys) -> None:
    packet = _build_packet(keys)

    # Must not raise.
    verify_signature(_signing_bytes_of(packet), packet["signature"], keys.signing_public)

    plaintext = decrypt_payload(
        encrypted_key=packet["encrypted_key"],
        nonce=packet["nonce"],
        ciphertext=packet["ciphertext"],
        aad=packet["aad"],
        recipient_private_key=keys.rsa_private,
    )
    assert json.loads(plaintext) == packet["instruction"]


def test_instruction_is_not_recoverable_from_the_wire_bytes(keys) -> None:
    """The payee id and amount must not appear in the transmitted packet."""
    packet = _build_packet(keys, _instruction(amount_minor=999_99))
    wire = packet["ciphertext"] + packet["encrypted_key"] + packet["aad"]
    assert b"device-bob" not in wire
    assert b"99999" not in wire


# --- Required test 2: a tampered payload is rejected ------------------------


def test_tampered_ciphertext_fails_signature_verification(keys) -> None:
    packet = _build_packet(keys)
    packet["ciphertext"] = _flip_last_byte(packet["ciphertext"])  # type: ignore[arg-type]

    with pytest.raises(SignatureVerificationError):
        verify_signature(_signing_bytes_of(packet), packet["signature"], keys.signing_public)


@pytest.mark.parametrize("field", ["packet_id", "sender_id", "created_at"])
def test_tampered_header_field_fails_signature_verification(keys, field: str) -> None:
    """Every field inside the signed region must be covered by the signature."""
    packet = _build_packet(keys)
    packet[field] = str(packet[field]) + "x"

    with pytest.raises(SignatureVerificationError):
        verify_signature(_signing_bytes_of(packet), packet["signature"], keys.signing_public)


@pytest.mark.parametrize("field", ["encrypted_key", "nonce", "aad"])
def test_tampered_envelope_bytes_fail_signature_verification(keys, field: str) -> None:
    packet = _build_packet(keys)
    packet[field] = _flip_last_byte(packet[field])  # type: ignore[arg-type]

    with pytest.raises(SignatureVerificationError):
        verify_signature(_signing_bytes_of(packet), packet["signature"], keys.signing_public)


def test_tampered_ciphertext_also_fails_gcm_tag_check(keys) -> None:
    """Even if a signature check were skipped, AES-GCM still catches the tamper."""
    packet = _build_packet(keys)
    packet["ciphertext"] = _flip_last_byte(packet["ciphertext"])  # type: ignore[arg-type]

    with pytest.raises(DecryptionError):
        decrypt_payload(
            encrypted_key=packet["encrypted_key"],
            nonce=packet["nonce"],
            ciphertext=packet["ciphertext"],
            aad=packet["aad"],
            recipient_private_key=keys.rsa_private,
        )


def test_swapped_aad_fails_decryption(keys) -> None:
    """A valid ciphertext re-headered under a different packet id must not open.

    This is the replay path the AAD binding exists to close.
    """
    packet = _build_packet(keys)
    forged_aad = build_aad(packet_id=str(uuid4()), sender_id="device-alice")

    with pytest.raises(DecryptionError):
        decrypt_payload(
            encrypted_key=packet["encrypted_key"],
            nonce=packet["nonce"],
            ciphertext=packet["ciphertext"],
            aad=forged_aad,
            recipient_private_key=keys.rsa_private,
        )


def test_wrong_rsa_key_cannot_unwrap_content_key(keys, other_keys) -> None:
    packet = _build_packet(keys)

    with pytest.raises(DecryptionError):
        decrypt_payload(
            encrypted_key=packet["encrypted_key"],
            nonce=packet["nonce"],
            ciphertext=packet["ciphertext"],
            aad=packet["aad"],
            recipient_private_key=other_keys.rsa_private,
        )


# --- Required test 3: a tampered signature is rejected ----------------------


def test_tampered_signature_is_rejected(keys) -> None:
    packet = _build_packet(keys)
    packet["signature"] = _flip_last_byte(packet["signature"])  # type: ignore[arg-type]

    with pytest.raises(SignatureVerificationError):
        verify_signature(_signing_bytes_of(packet), packet["signature"], keys.signing_public)


def test_truncated_signature_is_rejected(keys) -> None:
    packet = _build_packet(keys)
    packet["signature"] = packet["signature"][:-1]  # type: ignore[index]

    with pytest.raises(SignatureVerificationError):
        verify_signature(_signing_bytes_of(packet), packet["signature"], keys.signing_public)


def test_empty_signature_is_rejected(keys) -> None:
    packet = _build_packet(keys)

    with pytest.raises(SignatureVerificationError):
        verify_signature(_signing_bytes_of(packet), b"", keys.signing_public)


def test_signature_from_a_different_signer_is_rejected(keys, other_keys) -> None:
    """A forged packet signed by an unregistered key must not verify."""
    packet = _build_packet(keys)
    signing_bytes = _signing_bytes_of(packet)
    forged = sign_bytes(signing_bytes, other_keys.signing_private)

    with pytest.raises(SignatureVerificationError):
        verify_signature(signing_bytes, forged, keys.signing_public)


def test_signature_is_64_bytes(keys) -> None:
    """Ed25519 signatures are fixed size; a change here means the scheme changed."""
    packet = _build_packet(keys)
    assert len(packet["signature"]) == 64  # type: ignore[arg-type]


# --- Canonical serialization -------------------------------------------------


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_canonical_json_has_no_insignificant_whitespace() -> None:
    assert canonical_json({"a": 1, "b": "x"}) == b'{"a":1,"b":"x"}'


def test_canonical_json_survives_a_json_round_trip() -> None:
    """The queue transports JSON, so serialization must be stable across it."""
    original = {"packet_id": str(uuid4()), "amount_minor": 1234, "currency": "INR"}
    first = canonical_json(original)
    second = canonical_json(json.loads(first))
    assert first == second


def test_canonical_json_distinguishes_string_from_int() -> None:
    assert canonical_json({"amount_minor": 100}) != canonical_json({"amount_minor": "100"})


# --- base64url encoding ------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 2, 3, 16, 31, 32, 64, 384])
def test_b64u_round_trip(size: int) -> None:
    import os

    raw = os.urandom(size)
    assert b64u_decode(b64u_encode(raw)) == raw


def test_b64u_encode_is_url_safe_and_unpadded() -> None:
    encoded = b64u_encode(bytes(range(256)))
    assert "=" not in encoded
    assert "+" not in encoded
    assert "/" not in encoded


def test_b64u_decode_rejects_invalid_input() -> None:
    with pytest.raises(ValueError):
        b64u_decode("!!!not-base64!!!")


# --- Idempotency key ---------------------------------------------------------


def test_idempotency_key_is_stable_for_the_same_signed_region(keys) -> None:
    """The same packet arriving twice must produce the same dedupe key."""
    packet = _build_packet(keys)
    assert derive_idempotency_key(_signing_bytes_of(packet)) == derive_idempotency_key(
        _signing_bytes_of(packet)
    )


def test_idempotency_key_ignores_hop_metadata(keys) -> None:
    """Two copies down different mesh paths must dedupe to one settlement.

    `hops` is outside the signed region, so adding hop records must not
    change the key.
    """
    packet = _build_packet(keys)
    key_before = derive_idempotency_key(_signing_bytes_of(packet))

    packet["hops"] = [{"node": "relay-1"}, {"node": "relay-2"}]
    key_after = derive_idempotency_key(_signing_bytes_of(packet))

    assert key_before == key_after


def test_idempotency_key_differs_for_different_packets(keys) -> None:
    a = derive_idempotency_key(_signing_bytes_of(_build_packet(keys)))
    b = derive_idempotency_key(_signing_bytes_of(_build_packet(keys)))
    assert a != b


def test_idempotency_key_has_expected_shape(keys) -> None:
    key = derive_idempotency_key(_signing_bytes_of(_build_packet(keys)))
    assert key.startswith("settle:")
    assert len(key.removeprefix("settle:")) == 64  # sha256 hex


# --- Envelope parameters -----------------------------------------------------


def test_nonce_and_content_key_are_fresh_per_packet(keys) -> None:
    """Nonce reuse under the same key would break GCM confidentiality."""
    aad = build_aad(packet_id=str(uuid4()), sender_id="device-alice")
    payloads = [encrypt_payload(b"same plaintext", keys.rsa_public, aad) for _ in range(25)]

    assert len({p.nonce for p in payloads}) == 25
    assert len({p.encrypted_key for p in payloads}) == 25
    assert len({p.ciphertext for p in payloads}) == 25


def test_nonce_is_96_bits(keys) -> None:
    aad = build_aad(packet_id=str(uuid4()), sender_id="device-alice")
    payload = encrypt_payload(b"x", keys.rsa_public, aad)
    assert len(payload.nonce) == GCM_NONCE_BYTES == 12


def test_content_key_is_256_bits(keys) -> None:
    """Verified indirectly: the unwrapped key length is checked on decrypt."""
    assert AES_KEY_BYTES == 32


def test_empty_plaintext_round_trips(keys) -> None:
    aad = build_aad(packet_id=str(uuid4()), sender_id="device-alice")
    payload = encrypt_payload(b"", keys.rsa_public, aad)
    assert (
        decrypt_payload(
            encrypted_key=payload.encrypted_key,
            nonce=payload.nonce,
            ciphertext=payload.ciphertext,
            aad=payload.aad,
            recipient_private_key=keys.rsa_private,
        )
        == b""
    )


# --- Key persistence and loading --------------------------------------------


def test_keys_reload_from_pem_identically(keys) -> None:
    """Simulates a service restart: the same PEM must yield a usable keypair.

    Keys are persisted, not regenerated, so a signature made before a
    "restart" must still verify after it.
    """
    signing_bytes = b"payload that was signed before the restart"
    signature = sign_bytes(signing_bytes, load_signing_private_key(keys.signing_private_pem))

    # Fresh load of the same PEM text, as a restarted process would do.
    reloaded_public = load_signing_public_key(keys.signing_public_pem)
    verify_signature(signing_bytes, signature, reloaded_public)


def test_generated_keypairs_are_distinct() -> None:
    first_private, first_public = generate_signing_keypair()
    second_private, second_public = generate_signing_keypair()
    assert first_private != second_private
    assert first_public != second_public


def test_generated_pems_have_expected_headers() -> None:
    signing_private, signing_public = generate_signing_keypair()
    rsa_private, rsa_public = generate_rsa_keypair()
    assert signing_private.startswith("-----BEGIN PRIVATE KEY-----")
    assert signing_public.startswith("-----BEGIN PUBLIC KEY-----")
    assert rsa_private.startswith("-----BEGIN PRIVATE KEY-----")
    assert rsa_public.startswith("-----BEGIN PUBLIC KEY-----")


@pytest.mark.parametrize(
    "loader",
    [
        load_signing_private_key,
        load_signing_public_key,
        load_rsa_private_key,
        load_rsa_public_key,
    ],
)
def test_empty_pem_raises_key_configuration_error(loader) -> None:
    """A missing key must fail loudly at load time, not silently at use time."""
    with pytest.raises(KeyConfigurationError):
        loader("")


@pytest.mark.parametrize(
    "loader",
    [
        load_signing_private_key,
        load_signing_public_key,
        load_rsa_private_key,
        load_rsa_public_key,
    ],
)
def test_garbage_pem_raises_key_configuration_error(loader) -> None:
    with pytest.raises(KeyConfigurationError):
        loader("-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n")


def test_wrong_key_type_is_rejected(keys) -> None:
    """An RSA key supplied where Ed25519 is expected must be refused."""
    with pytest.raises(KeyConfigurationError):
        load_signing_private_key(keys.rsa_private_pem)

    with pytest.raises(KeyConfigurationError):
        load_rsa_private_key(keys.signing_private_pem)

    with pytest.raises(KeyConfigurationError):
        load_signing_public_key(keys.rsa_public_pem)

    with pytest.raises(KeyConfigurationError):
        load_rsa_public_key(keys.signing_public_pem)


def test_key_errors_do_not_leak_key_material(keys) -> None:
    """Error messages must never contain key bytes (RULES.md: never log secrets)."""
    try:
        load_signing_private_key(keys.rsa_private_pem)
    except KeyConfigurationError as exc:
        message = str(exc)
        assert "BEGIN" not in message
        assert "MII" not in message  # common base64 DER prefix
    else:
        pytest.fail("expected KeyConfigurationError")
