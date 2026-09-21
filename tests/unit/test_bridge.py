"""Unit tests for the bridge node.

The bridge is the boundary between the offline simulation and settlement. Its
job: verify, then durably enqueue. It must never settle, never mutate the
signed region, and never report success for a message the broker refused.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi import FastAPI

from services.bridge.app import create_app, get_publisher, get_sender_public_key
from services.bridge.publisher import InMemoryPublisher, PublishError
from shared.models import ErrorCode, HopRecord, PacketStatus, PaymentInstruction, PaymentPacket


def _packet(keys, hops: int = 2) -> PaymentPacket:
    instruction = PaymentInstruction(
        packet_id=uuid4(),
        payer_id="device-alice",
        payee_id="device-bob",
        amount_minor=125_00,
        currency="INR",
        created_at=datetime.now(UTC),
    )
    packet = PaymentPacket.create(
        instruction=instruction,
        sender_id="device-alice",
        signing_key=keys.signing_private,
        recipient_public_key=keys.rsa_public,
    )
    for index in range(hops):
        packet = packet.append_hop(
            HopRecord(node_id=f"relay-{index}", received_at=datetime.now(UTC))
        )
    return packet


class FailingPublisher:
    """A publisher whose broker never confirms."""

    queue_name = "meshsettle.settlements"

    async def publish(self, body: bytes, *, message_id: str) -> None:
        raise PublishError("broker did not confirm the message")


def _bridge_app(keys, publisher: object) -> FastAPI:
    app = create_app()
    app.dependency_overrides[get_sender_public_key] = lambda: keys.signing_public
    app.dependency_overrides[get_publisher] = lambda: publisher
    return app


async def _post_packet(app: FastAPI, packet: PaymentPacket) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        return await client.post(
            "/bridge",
            content=packet.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )


# --- Health ------------------------------------------------------------------


async def test_healthz_reports_ok(keys) -> None:
    app = _bridge_app(keys, InMemoryPublisher())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "bridge"}


# --- Publishing --------------------------------------------------------------


async def test_bridge_publishes_the_packet_to_the_queue(keys) -> None:
    publisher = InMemoryPublisher()
    packet = _packet(keys)

    response = await _post_packet(_bridge_app(keys, publisher), packet)

    assert response.status_code == 202
    assert len(publisher.messages) == 1

    body = response.json()
    assert body["status"] == PacketStatus.BRIDGED.value
    assert body["packet_id"] == str(packet.packet_id)
    assert body["idempotency_key"] == packet.idempotency_key
    assert body["hop_count"] == 2
    assert body["queue"] == publisher.queue_name


async def test_published_message_preserves_the_signed_region(keys) -> None:
    publisher = InMemoryPublisher()
    packet = _packet(keys)

    await _post_packet(_bridge_app(keys, publisher), packet)

    queued_body, _message_id = publisher.messages[0]
    queued = PaymentPacket.model_validate_json(queued_body)

    assert queued.signing_bytes == packet.signing_bytes
    assert queued.signature == packet.signature
    assert queued.idempotency_key == packet.idempotency_key
    assert queued.envelope == packet.envelope
    queued.verify(keys.signing_public)


async def test_published_message_id_is_the_idempotency_key(keys) -> None:
    """The broker-level id makes duplicates visible in broker tooling."""
    publisher = InMemoryPublisher()
    packet = _packet(keys)

    await _post_packet(_bridge_app(keys, publisher), packet)

    _body, message_id = publisher.messages[0]
    assert message_id == packet.idempotency_key


async def test_published_message_retains_hop_history(keys) -> None:
    publisher = InMemoryPublisher()
    packet = _packet(keys, hops=3)

    await _post_packet(_bridge_app(keys, publisher), packet)

    queued = PaymentPacket.model_validate_json(publisher.messages[0][0])
    assert [hop.node_id for hop in queued.hops] == ["relay-0", "relay-1", "relay-2"]


async def test_bridge_publishes_duplicates_without_deduplicating(keys) -> None:
    """Dedupe is settlement's job, not the bridge's.

    The bridge must not silently swallow a duplicate: the whole point is that
    settlement proves exactly-once even when the queue delivers repeats.
    """
    publisher = InMemoryPublisher()
    packet = _packet(keys)
    app = _bridge_app(keys, publisher)

    for _ in range(5):
        response = await _post_packet(app, packet)
        assert response.status_code == 202

    assert len(publisher.messages) == 5
    assert len({message_id for _body, message_id in publisher.messages}) == 1


async def test_bridge_cannot_read_the_payment(keys) -> None:
    publisher = InMemoryPublisher()
    await _post_packet(_bridge_app(keys, publisher), _packet(keys))

    queued_body = publisher.messages[0][0]
    assert b"device-bob" not in queued_body
    assert b"12500" not in queued_body


# --- Rejection ---------------------------------------------------------------


async def test_bridge_rejects_a_tampered_packet_and_publishes_nothing(keys) -> None:
    publisher = InMemoryPublisher()
    packet = _packet(keys)
    payload = json.loads(packet.model_dump_json())
    payload["packet_id"] = str(uuid4())

    transport = httpx.ASGITransport(app=_bridge_app(keys, publisher))
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        response = await client.post("/bridge", json=payload)

    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.INVALID_SIGNATURE.value
    assert publisher.messages == []


async def test_bridge_rejects_a_packet_from_an_unknown_signer(keys, other_keys) -> None:
    publisher = InMemoryPublisher()
    forged = PaymentPacket.create(
        instruction=PaymentInstruction(
            packet_id=uuid4(),
            payer_id="device-alice",
            payee_id="device-bob",
            amount_minor=100,
            currency="INR",
            created_at=datetime.now(UTC),
        ),
        sender_id="device-alice",
        signing_key=other_keys.signing_private,
        recipient_public_key=keys.rsa_public,
    )

    response = await _post_packet(_bridge_app(keys, publisher), forged)

    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.INVALID_SIGNATURE.value
    assert publisher.messages == []


async def test_bridge_rejects_malformed_payload(keys) -> None:
    publisher = InMemoryPublisher()
    transport = httpx.ASGITransport(app=_bridge_app(keys, publisher))
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as client:
        response = await client.post("/bridge", json={"packet_id": "not-a-uuid"})

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.MALFORMED_PAYLOAD.value
    assert publisher.messages == []


async def test_bridge_reports_unavailable_when_the_broker_will_not_confirm(keys) -> None:
    """An unconfirmed publish must surface as a failure, not a false success."""
    response = await _post_packet(_bridge_app(keys, FailingPublisher()), _packet(keys))

    assert response.status_code == 503
    assert response.json()["code"] == ErrorCode.INTERNAL_ERROR.value
