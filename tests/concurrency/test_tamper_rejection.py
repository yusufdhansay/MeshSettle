"""Tamper detection: corrupted packets must never settle.

TASK.md requires firing N corrupted packets and asserting that 0 settle and
all are logged as rejected. This file does that across every field an attacker
could realistically touch, and asserts on durable evidence in the
``rejected_packets`` table rather than on log scraping.

The corruption cases are grouped by which defence is expected to catch them:

* signature coverage catches changes to the header and envelope
* AES-GCM's authentication tag catches ciphertext and AAD edits
* the inner/outer packet id cross-check catches a validly signed header
  wrapped around an instruction for a different packet
* schema validation catches structurally impossible packets
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.settlement.db import count_rejections, count_settlements
from services.settlement.processor import SettlementProcessor
from shared.crypto import b64u_encode, build_aad, build_signing_bytes, encrypt_payload, sign_bytes
from shared.models import (
    EncryptedEnvelope,
    ErrorCode,
    PacketStatus,
    PaymentPacket,
    canonical_timestamp,
)
from tests.concurrency.conftest import build_packet

pytestmark = pytest.mark.integration

#: Corrupted packets fired in the bulk test.
TAMPERED_COUNT = 50


def _flip_last_byte(raw: bytes) -> bytes:
    return raw[:-1] + bytes([raw[-1] ^ 0x01])


# --- Corruption builders -----------------------------------------------------


def corrupt_signature(packet: PaymentPacket) -> dict:
    payload = json.loads(packet.model_dump_json())
    payload["signature"] = b64u_encode(_flip_last_byte(packet.signature))
    return payload


def corrupt_sender_id(packet: PaymentPacket) -> dict:
    payload = json.loads(packet.model_dump_json())
    payload["sender_id"] = "device-attacker"
    return payload


def corrupt_packet_id(packet: PaymentPacket) -> dict:
    payload = json.loads(packet.model_dump_json())
    payload["packet_id"] = str(uuid4())
    return payload


def corrupt_created_at(packet: PaymentPacket) -> dict:
    payload = json.loads(packet.model_dump_json())
    payload["created_at"] = canonical_timestamp(datetime(2020, 1, 1, tzinfo=UTC))
    return payload


def corrupt_ciphertext(packet: PaymentPacket) -> dict:
    payload = json.loads(packet.model_dump_json())
    payload["envelope"]["ciphertext"] = b64u_encode(_flip_last_byte(packet.envelope.ciphertext))
    return payload


def corrupt_encrypted_key(packet: PaymentPacket) -> dict:
    payload = json.loads(packet.model_dump_json())
    payload["envelope"]["encrypted_key"] = b64u_encode(
        _flip_last_byte(packet.envelope.encrypted_key)
    )
    return payload


def corrupt_nonce(packet: PaymentPacket) -> dict:
    payload = json.loads(packet.model_dump_json())
    payload["envelope"]["nonce"] = b64u_encode(_flip_last_byte(packet.envelope.nonce))
    return payload


def corrupt_aad(packet: PaymentPacket) -> dict:
    payload = json.loads(packet.model_dump_json())
    payload["envelope"]["aad"] = b64u_encode(_flip_last_byte(packet.envelope.aad))
    return payload


CORRUPTIONS = {
    "signature_bit_flip": corrupt_signature,
    "sender_id_swap": corrupt_sender_id,
    "packet_id_swap": corrupt_packet_id,
    "created_at_rewrite": corrupt_created_at,
    "ciphertext_bit_flip": corrupt_ciphertext,
    "encrypted_key_bit_flip": corrupt_encrypted_key,
    "nonce_bit_flip": corrupt_nonce,
    "aad_bit_flip": corrupt_aad,
}


# --- The headline requirement ------------------------------------------------


async def test_fifty_corrupted_packets_all_rejected_and_none_settle(
    processor: SettlementProcessor, session_factory, keys
) -> None:
    """Fire N corrupted packets: 0 settle, N recorded as rejected."""
    corruption_names = list(CORRUPTIONS)
    bodies: list[bytes] = []
    for index in range(TAMPERED_COUNT):
        packet = build_packet(keys, amount_minor=1_000 + index)
        corrupt = CORRUPTIONS[corruption_names[index % len(corruption_names)]]
        bodies.append(json.dumps(corrupt(packet)).encode("utf-8"))

    outcomes = await asyncio.gather(*(processor.process(body) for body in bodies))

    assert len(outcomes) == TAMPERED_COUNT
    assert all(not outcome.settled for outcome in outcomes), "no corrupted packet may settle"
    assert all(outcome.status is PacketStatus.REJECTED for outcome in outcomes)

    async with session_factory() as session:
        assert await count_settlements(session) == 0
        # Every rejection left durable evidence.
        assert await count_rejections(session) == TAMPERED_COUNT

    assert processor.metrics.settled == 0
    assert processor.metrics.rejected == TAMPERED_COUNT


@pytest.mark.parametrize("corruption_name", sorted(CORRUPTIONS))
async def test_each_corruption_is_rejected(
    processor: SettlementProcessor, session_factory, keys, corruption_name: str
) -> None:
    """Every individually corrupted field must be caught."""
    packet = build_packet(keys)
    body = json.dumps(CORRUPTIONS[corruption_name](packet)).encode("utf-8")

    outcome = await processor.process(body)

    assert not outcome.settled
    assert outcome.code in {
        ErrorCode.INVALID_SIGNATURE,
        ErrorCode.DECRYPTION_FAILED,
        ErrorCode.MALFORMED_PAYLOAD,
    }
    async with session_factory() as session:
        assert await count_settlements(session) == 0
        assert await count_rejections(session) >= 1


@pytest.mark.parametrize("corruption_name", sorted(CORRUPTIONS))
async def test_signed_region_corruptions_are_caught_by_the_signature(
    processor: SettlementProcessor, keys, corruption_name: str
) -> None:
    """Everything in the signed region should fail signature verification.

    All eight corrupted fields are inside the signed region, so the signature
    check is the layer that should catch each one. If any of these started
    reporting DECRYPTION_FAILED instead, it would mean the signature no longer
    covers that field.
    """
    packet = build_packet(keys)
    body = json.dumps(CORRUPTIONS[corruption_name](packet)).encode("utf-8")

    outcome = await processor.process(body)
    assert outcome.code is ErrorCode.INVALID_SIGNATURE


# --- Forgery -----------------------------------------------------------------


async def test_packet_signed_by_an_unknown_key_is_rejected(
    processor: SettlementProcessor, session_factory, keys, other_keys
) -> None:
    """A well-formed packet from an unregistered signer must not settle."""
    forged = build_packet(other_keys)  # signed and sealed with the wrong keypair

    outcome = await processor.process(forged.model_dump_json().encode("utf-8"))

    assert not outcome.settled
    assert outcome.code is ErrorCode.INVALID_SIGNATURE
    async with session_factory() as session:
        assert await count_settlements(session) == 0
        assert await count_rejections(session, ErrorCode.INVALID_SIGNATURE.value) == 1


async def test_replayed_ciphertext_under_a_new_header_is_rejected(
    processor: SettlementProcessor, session_factory, dedupe, keys
) -> None:
    """Lift a sealed payload onto a fresh, validly signed header.

    The attacker signs a new header around someone else's envelope and carries
    the original AAD along, so the signature is genuine and the ciphertext
    would decrypt cleanly. What stops it is the AAD/header consistency check:
    the AAD names the original packet_id, the header names a new one.

    Worth noting what does *not* stop it: the GCM tag alone. Because the
    attacker presents the AAD the ciphertext was sealed with, decryption
    succeeds. Comparing the AAD against the header is what makes the binding
    load-bearing, and it rejects the replay before it consumes an
    idempotency key.
    """
    original = build_packet(keys)

    new_packet_id = uuid4()
    created_at = datetime.now(UTC)
    # Re-sign a header carrying the ORIGINAL envelope but a NEW packet id.
    signing_bytes = build_signing_bytes(
        packet_id=str(new_packet_id),
        sender_id=original.sender_id,
        created_at=canonical_timestamp(created_at),
        encrypted_key=original.envelope.encrypted_key,
        nonce=original.envelope.nonce,
        ciphertext=original.envelope.ciphertext,
        aad=original.envelope.aad,
    )
    replay = PaymentPacket(
        packet_id=new_packet_id,
        sender_id=original.sender_id,
        created_at=created_at,
        envelope=original.envelope,
        signature=sign_bytes(signing_bytes, keys.signing_private),
    )

    # The signature really is valid; only the AAD binding catches this.
    replay.verify(keys.signing_public)

    outcome = await processor.process(replay.model_dump_json().encode("utf-8"))
    assert not outcome.settled
    assert outcome.code is ErrorCode.MALFORMED_PAYLOAD
    assert "AAD" in outcome.detail
    async with session_factory() as session:
        assert await count_settlements(session) == 0

    # Rejected before the claim, so the key is still available to the real packet.
    assert await dedupe.state(replay.idempotency_key) is None


async def test_inner_outer_packet_id_mismatch_is_rejected(
    processor: SettlementProcessor, session_factory, keys
) -> None:
    """A validly signed header wrapped around another packet's instruction.

    Everything checks out except the cross-check: signature valid, AAD
    consistent with the header, ciphertext opens cleanly. Only comparing the
    decrypted instruction's packet_id against the header's catches it.
    """
    from shared.models import PaymentInstruction

    header_id = uuid4()
    inner = PaymentInstruction(
        packet_id=uuid4(),  # deliberately different
        payer_id="device-alice",
        payee_id="device-bob",
        amount_minor=500_00,
        currency="INR",
        created_at=datetime.now(UTC),
    )
    aad = build_aad(packet_id=str(header_id), sender_id="device-alice")
    payload = encrypt_payload(inner.to_canonical_bytes(), keys.rsa_public, aad)

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
    packet.verify(keys.signing_public)  # genuinely valid signature

    outcome = await processor.process(packet.model_dump_json().encode("utf-8"))

    assert not outcome.settled
    assert outcome.code is ErrorCode.MALFORMED_PAYLOAD
    assert "inner packet_id" in outcome.detail
    async with session_factory() as session:
        assert await count_settlements(session) == 0


# --- Malformed input ---------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json at all",
        b"{}",
        b"[]",
        b'{"packet_id": "not-a-uuid"}',
        b"null",
        b'{"packet_id": "' + str(uuid4()).encode() + b'", "sender_id": "device-alice"}',
    ],
)
async def test_malformed_bodies_are_rejected(
    processor: SettlementProcessor, session_factory, body: bytes
) -> None:
    outcome = await processor.process(body)

    assert not outcome.settled
    assert outcome.code is ErrorCode.MALFORMED_PAYLOAD
    async with session_factory() as session:
        assert await count_settlements(session) == 0
        assert await count_rejections(session, ErrorCode.MALFORMED_PAYLOAD.value) >= 1


async def test_zero_amount_cannot_be_constructed_or_settled(keys) -> None:
    """Money validation happens at the schema boundary, before settlement."""
    from pydantic import ValidationError

    from shared.models import PaymentInstruction

    with pytest.raises(ValidationError):
        PaymentInstruction(
            packet_id=uuid4(),
            payer_id="device-alice",
            payee_id="device-bob",
            amount_minor=0,
            currency="INR",
            created_at=datetime.now(UTC),
        )


# --- Tampering must not poison legitimate settlement ------------------------


async def test_a_tampered_copy_does_not_block_the_genuine_packet(
    processor: SettlementProcessor, session_factory, keys
) -> None:
    """An attacker must not be able to burn a packet's idempotency key.

    This is why the signature check precedes the Redis claim: if claiming came
    first, anyone could submit a corrupted copy, consume the key, and make the
    real payment look like a duplicate.
    """
    genuine = build_packet(keys)
    tampered = json.dumps(corrupt_ciphertext(genuine)).encode("utf-8")

    rejected = await processor.process(tampered)
    assert not rejected.settled
    assert rejected.code is ErrorCode.INVALID_SIGNATURE

    settled = await processor.process(genuine.model_dump_json().encode("utf-8"))
    assert settled.settled, "a tampered copy must not prevent the real packet settling"

    async with session_factory() as session:
        assert await count_settlements(session, genuine.idempotency_key) == 1


async def test_tampered_and_genuine_arriving_together_settles_once(
    processor: SettlementProcessor, session_factory, keys
) -> None:
    """Concurrent mix of one genuine packet and many corrupted copies."""
    genuine = build_packet(keys)
    genuine_body = genuine.model_dump_json().encode("utf-8")
    bodies = [genuine_body]
    for corrupt in CORRUPTIONS.values():
        bodies.append(json.dumps(corrupt(genuine)).encode("utf-8"))

    outcomes = await asyncio.gather(*(processor.process(body) for body in bodies))

    assert sum(1 for outcome in outcomes if outcome.settled) == 1
    async with session_factory() as session:
        assert await count_settlements(session) == 1


async def test_rejections_record_a_specific_reason(
    processor: SettlementProcessor, session_factory, keys
) -> None:
    """Rejections must be distinguishable by reason, not lumped together."""
    await processor.process(json.dumps(corrupt_signature(build_packet(keys))).encode("utf-8"))
    await processor.process(b"not json")

    async with session_factory() as session:
        assert await count_rejections(session, ErrorCode.INVALID_SIGNATURE.value) == 1
        assert await count_rejections(session, ErrorCode.MALFORMED_PAYLOAD.value) == 1
