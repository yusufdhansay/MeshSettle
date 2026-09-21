"""Unit tests for the shared packet models.

These guard the wire contract: field validation, byte encoding, canonical
timestamp handling, immutability, and the two properties settlement depends
on (signature covers the header + envelope; hops do not affect the
idempotency key).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared.crypto import DecryptionError, SignatureVerificationError
from shared.models import (
    MAX_AMOUNT_MINOR,
    EncryptedEnvelope,
    ErrorCode,
    HopRecord,
    PacketCreatedResponse,
    PacketStatus,
    PaymentInstruction,
    PaymentPacket,
    TransactionRequest,
    canonical_timestamp,
)


def _instruction(**overrides) -> PaymentInstruction:
    data = {
        "packet_id": uuid4(),
        "payer_id": "device-alice",
        "payee_id": "device-bob",
        "amount_minor": 125_00,
        "currency": "INR",
        "created_at": datetime.now(UTC),
    }
    data.update(overrides)
    return PaymentInstruction(**data)


def _packet(keys, **overrides) -> PaymentPacket:
    instruction = overrides.pop("instruction", None) or _instruction()
    return PaymentPacket.create(
        instruction=instruction,
        sender_id=overrides.pop("sender_id", "device-alice"),
        signing_key=keys.signing_private,
        recipient_public_key=keys.rsa_public,
        **overrides,
    )


# --- PaymentInstruction ------------------------------------------------------


def test_instruction_round_trips_through_canonical_bytes() -> None:
    instruction = _instruction()
    assert PaymentInstruction.from_canonical_bytes(instruction.to_canonical_bytes()) == instruction


def test_instruction_canonical_bytes_are_deterministic() -> None:
    instruction = _instruction()
    assert instruction.to_canonical_bytes() == instruction.to_canonical_bytes()


@pytest.mark.parametrize("amount", [0, -1, -12345, MAX_AMOUNT_MINOR + 1])
def test_instruction_rejects_invalid_amounts(amount: int) -> None:
    with pytest.raises(ValidationError):
        _instruction(amount_minor=amount)


def test_instruction_accepts_boundary_amounts() -> None:
    assert _instruction(amount_minor=1).amount_minor == 1
    assert _instruction(amount_minor=MAX_AMOUNT_MINOR).amount_minor == MAX_AMOUNT_MINOR


def test_instruction_rejects_float_amount() -> None:
    """Money is integer minor units. A fractional amount must not slip through."""
    with pytest.raises(ValidationError):
        _instruction(amount_minor=12.5)


def test_instruction_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        _instruction(created_at=datetime(2026, 9, 21, 12, 0, 0))  # noqa: DTZ001


def test_instruction_normalizes_timestamp_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    instruction = _instruction(created_at=datetime(2026, 9, 21, 17, 30, 0, tzinfo=ist))
    assert instruction.created_at.tzinfo == UTC
    assert instruction.created_at == datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def test_instruction_rejects_same_payer_and_payee() -> None:
    with pytest.raises(ValidationError):
        _instruction(payer_id="device-alice", payee_id="device-alice")


@pytest.mark.parametrize(
    "bad_id",
    ["", "a" * 65, "has space", "semi;colon", "new\nline", 'quote"d', "<script>"],
)
def test_instruction_rejects_malformed_identifiers(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        _instruction(payer_id=bad_id)


@pytest.mark.parametrize("bad_currency", ["inr", "INRX", "IN", "1NR", ""])
def test_instruction_rejects_malformed_currency(bad_currency: str) -> None:
    with pytest.raises(ValidationError):
        _instruction(currency=bad_currency)


def test_instruction_rejects_unknown_fields() -> None:
    """extra=forbid stops a caller smuggling unvalidated data into the payload."""
    with pytest.raises(ValidationError):
        PaymentInstruction(
            packet_id=uuid4(),
            payer_id="device-alice",
            payee_id="device-bob",
            amount_minor=100,
            currency="INR",
            created_at=datetime.now(UTC),
            surprise="payload",
        )


def test_instruction_is_immutable() -> None:
    instruction = _instruction()
    with pytest.raises(ValidationError):
        instruction.amount_minor = 999_999  # type: ignore[misc]


def test_from_canonical_bytes_rejects_non_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        PaymentInstruction.from_canonical_bytes(b"not json at all")


def test_from_canonical_bytes_rejects_json_non_object() -> None:
    with pytest.raises(ValueError, match="not a JSON object"):
        PaymentInstruction.from_canonical_bytes(b"[1, 2, 3]")


# --- Canonical timestamps ----------------------------------------------------


def test_canonical_timestamp_is_stable_across_equal_instants() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    same_instant_utc = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
    same_instant_ist = datetime(2026, 9, 21, 17, 30, 0, tzinfo=ist)
    assert canonical_timestamp(same_instant_utc) == canonical_timestamp(same_instant_ist)


def test_canonical_timestamp_is_utc_offset_form() -> None:
    assert canonical_timestamp(datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)).endswith("+00:00")


# --- EncryptedEnvelope -------------------------------------------------------


def test_envelope_rejects_wrong_nonce_length(keys) -> None:
    packet = _packet(keys)
    payload = packet.envelope.model_dump()
    payload["nonce"] = "AAAA"  # 3 bytes once decoded
    with pytest.raises(ValidationError):
        EncryptedEnvelope.model_validate(payload)


@pytest.mark.parametrize("field", ["encrypted_key", "ciphertext", "aad"])
def test_envelope_rejects_empty_byte_fields(keys, field: str) -> None:
    packet = _packet(keys)
    payload = packet.envelope.model_dump()
    payload[field] = ""
    with pytest.raises(ValidationError):
        EncryptedEnvelope.model_validate(payload)


def test_envelope_bytes_serialize_as_unpadded_base64url(keys) -> None:
    packet = _packet(keys)
    dumped = json.loads(packet.model_dump_json())["envelope"]
    for field in ("encrypted_key", "nonce", "ciphertext", "aad"):
        value = dumped[field]
        assert isinstance(value, str)
        assert "=" not in value and "+" not in value and "/" not in value


def test_envelope_rejects_invalid_base64(keys) -> None:
    packet = _packet(keys)
    payload = packet.envelope.model_dump()
    payload["ciphertext"] = "!!!not base64!!!"
    with pytest.raises(ValidationError):
        EncryptedEnvelope.model_validate(payload)


# --- PaymentPacket -----------------------------------------------------------


def test_created_packet_verifies_and_opens(keys) -> None:
    instruction = _instruction(amount_minor=42_00)
    packet = _packet(keys, instruction=instruction)

    packet.verify(keys.signing_public)
    assert packet.open_instruction(keys.rsa_private) == instruction


def test_packet_aad_matches_header(keys) -> None:
    packet = _packet(keys)
    assert packet.envelope.aad == packet.expected_aad()


def test_packet_json_round_trip_preserves_signature_validity(keys) -> None:
    """The queue carries JSON, so verification must survive the trip."""
    packet = _packet(keys)
    restored = PaymentPacket.model_validate_json(packet.model_dump_json())

    assert restored == packet
    restored.verify(keys.signing_public)
    assert restored.idempotency_key == packet.idempotency_key
    assert restored.open_instruction(keys.rsa_private) == packet.open_instruction(keys.rsa_private)


def test_idempotency_key_is_not_transmitted(keys) -> None:
    """Nodes must derive the dedupe key, never trust a supplied one."""
    packet = _packet(keys)
    assert "idempotency_key" not in json.loads(packet.model_dump_json())


def test_packet_rejects_unknown_fields(keys) -> None:
    packet = _packet(keys)
    payload = json.loads(packet.model_dump_json())
    payload["idempotency_key"] = "settle:deadbeef"
    with pytest.raises(ValidationError):
        PaymentPacket.model_validate(payload)


def test_packet_rejects_wrong_signature_length(keys) -> None:
    packet = _packet(keys)
    payload = json.loads(packet.model_dump_json())
    payload["signature"] = payload["signature"][:-4]
    with pytest.raises(ValidationError):
        PaymentPacket.model_validate(payload)


def test_packet_is_immutable(keys) -> None:
    packet = _packet(keys)
    with pytest.raises(ValidationError):
        packet.sender_id = "device-attacker"  # type: ignore[misc]


def test_packet_rejects_malformed_sender_id(keys) -> None:
    packet = _packet(keys)
    payload = json.loads(packet.model_dump_json())
    payload["sender_id"] = "bad sender id with spaces"
    with pytest.raises(ValidationError):
        PaymentPacket.model_validate(payload)


def test_open_instruction_rejects_inner_outer_packet_id_mismatch(keys) -> None:
    """A header signed for one id must not carry an instruction for another.

    Built deliberately: seal an instruction whose inner id differs from the
    header id, with a signature that is otherwise perfectly valid.
    """
    from shared.crypto import build_aad, encrypt_payload

    header_id = uuid4()
    inner = _instruction(packet_id=uuid4())
    aad = build_aad(packet_id=str(header_id), sender_id="device-alice")
    payload = encrypt_payload(inner.to_canonical_bytes(), keys.rsa_public, aad)

    from shared.crypto import build_signing_bytes, sign_bytes

    created_at = datetime.now(UTC)
    signature = sign_bytes(
        build_signing_bytes(
            packet_id=str(header_id),
            sender_id="device-alice",
            created_at=canonical_timestamp(created_at),
            encrypted_key=payload.encrypted_key,
            nonce=payload.nonce,
            ciphertext=payload.ciphertext,
            aad=payload.aad,
        ),
        keys.signing_private,
    )
    packet = PaymentPacket(
        packet_id=header_id,
        sender_id="device-alice",
        created_at=created_at,
        envelope=EncryptedEnvelope(
            encrypted_key=payload.encrypted_key,
            nonce=payload.nonce,
            ciphertext=payload.ciphertext,
            aad=payload.aad,
        ),
        signature=signature,
    )

    # The signature is genuinely valid; only the cross-check catches this.
    packet.verify(keys.signing_public)
    with pytest.raises(ValueError, match="inner packet_id"):
        packet.open_instruction(keys.rsa_private)


def test_open_instruction_fails_on_tampered_ciphertext(keys) -> None:
    packet = _packet(keys)
    payload = json.loads(packet.model_dump_json())
    raw = bytearray(packet.envelope.ciphertext)
    raw[-1] ^= 0x01
    from shared.crypto import b64u_encode

    payload["envelope"]["ciphertext"] = b64u_encode(bytes(raw))
    tampered = PaymentPacket.model_validate(payload)

    with pytest.raises(SignatureVerificationError):
        tampered.verify(keys.signing_public)
    with pytest.raises(DecryptionError):
        tampered.open_instruction(keys.rsa_private)


def test_packet_from_other_signer_does_not_verify(keys, other_keys) -> None:
    forged = PaymentPacket.create(
        instruction=_instruction(),
        sender_id="device-alice",
        signing_key=other_keys.signing_private,
        recipient_public_key=keys.rsa_public,
    )
    with pytest.raises(SignatureVerificationError):
        forged.verify(keys.signing_public)


# --- Hops --------------------------------------------------------------------


def test_append_hop_preserves_signature_and_idempotency_key(keys) -> None:
    packet = _packet(keys)
    hopped = packet
    for index in range(5):
        hopped = hopped.append_hop(
            HopRecord(node_id=f"relay-{index}", received_at=datetime.now(UTC))
        )

    assert hopped.hop_count == 5
    assert hopped.signature == packet.signature
    assert hopped.signing_bytes == packet.signing_bytes
    assert hopped.idempotency_key == packet.idempotency_key
    hopped.verify(keys.signing_public)


def test_append_hop_does_not_mutate_the_original(keys) -> None:
    packet = _packet(keys)
    packet.append_hop(HopRecord(node_id="relay-1", received_at=datetime.now(UTC)))
    assert packet.hop_count == 0


def test_hops_survive_json_round_trip(keys) -> None:
    packet = _packet(keys).append_hop(HopRecord(node_id="relay-1", received_at=datetime.now(UTC)))
    restored = PaymentPacket.model_validate_json(packet.model_dump_json())
    assert restored.hop_count == 1
    assert restored.hops[0].node_id == "relay-1"
    restored.verify(keys.signing_public)


def test_hop_rejects_malformed_node_id() -> None:
    with pytest.raises(ValidationError):
        HopRecord(node_id="relay 1 with spaces", received_at=datetime.now(UTC))


def test_hop_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        HopRecord(node_id="relay-1", received_at=datetime(2026, 9, 21))  # noqa: DTZ001


# --- TransactionRequest ------------------------------------------------------


def test_transaction_request_builds_instruction_with_fresh_ids() -> None:
    request = TransactionRequest(payer_id="device-alice", payee_id="device-bob", amount_minor=500)
    first = request.to_instruction()
    second = request.to_instruction()

    assert first.packet_id != second.packet_id
    assert first.amount_minor == 500
    assert first.currency == "INR"


def test_transaction_request_defaults_currency_to_inr() -> None:
    request = TransactionRequest(payer_id="device-alice", payee_id="device-bob", amount_minor=1)
    assert request.currency == "INR"


def test_transaction_request_rejects_same_payer_and_payee() -> None:
    with pytest.raises(ValidationError):
        TransactionRequest(payer_id="device-alice", payee_id="device-alice", amount_minor=1)


def test_transaction_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TransactionRequest(
            payer_id="device-alice",
            payee_id="device-bob",
            amount_minor=1,
            amount_major=100,
        )


# --- Response models ---------------------------------------------------------


def test_packet_created_response_exposes_idempotency_key(keys) -> None:
    packet = _packet(keys)
    response = PacketCreatedResponse.of(packet)
    assert response.idempotency_key == packet.idempotency_key
    assert response.status is PacketStatus.CREATED


def test_error_codes_are_distinct_strings() -> None:
    values = [code.value for code in ErrorCode]
    assert len(values) == len(set(values))
    assert "INVALID_SIGNATURE" in values
    assert "DUPLICATE_PACKET" in values
