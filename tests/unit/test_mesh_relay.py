"""Unit tests for the mesh relay node.

The relay's contract is narrow and worth pinning down precisely: verify,
append an unsigned hop, forward byte-for-byte, never alter the signed region,
never make a settlement decision.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from services.mesh_relay.app import (
    RelayConfig,
    create_app,
    get_http_client,
    get_relay_config,
    get_sender_public_key,
)
from shared.models import ErrorCode, HopRecord, PacketStatus, PaymentInstruction, PaymentPacket


def _packet(keys, sender_id: str = "device-alice") -> PaymentPacket:
    instruction = PaymentInstruction(
        packet_id=uuid4(),
        payer_id="device-alice",
        payee_id="device-bob",
        amount_minor=125_00,
        currency="INR",
        created_at=datetime.now(UTC),
    )
    return PaymentPacket.create(
        instruction=instruction,
        sender_id=sender_id,
        signing_key=keys.signing_private,
        recipient_public_key=keys.rsa_public,
    )


class RecordingSink:
    """A stand-in for the next node. Records exactly what bytes it received."""

    def __init__(self, status_code: int = 202) -> None:
        self.status_code = status_code
        self.requests: list[tuple[str, bytes]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((str(request.url), request.content))
        return httpx.Response(self.status_code, json={"status": "ok"})

    @property
    def last_body(self) -> bytes:
        return self.requests[-1][1]

    @property
    def last_url(self) -> str:
        return self.requests[-1][0]


def _relay_app(
    keys,
    sink: RecordingSink,
    *,
    node_id: str = "relay-1",
    hop_count_target: int = 2,
    hop_limit: int = 16,
) -> FastAPI:
    """A relay app whose forwarding target is a recording sink."""
    app = create_app()
    config = RelayConfig(
        node_id=node_id,
        next_relay_url="http://relay-next",
        bridge_url="http://bridge-node",
        hop_count_target=hop_count_target,
        hop_limit=hop_limit,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(sink.handle))

    app.dependency_overrides[get_sender_public_key] = lambda: keys.signing_public
    app.dependency_overrides[get_relay_config] = lambda: config
    app.dependency_overrides[get_http_client] = lambda: client
    return app


async def _post_packet(app: FastAPI, packet: PaymentPacket) -> httpx.Response:
    """Send a packet to a relay app over an in-process ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay-under-test") as client:
        return await client.post(
            "/relay",
            content=packet.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )


# --- Health ------------------------------------------------------------------


async def test_healthz_reports_node_id(keys) -> None:
    sink = RecordingSink()
    app = _relay_app(keys, sink, node_id="relay-7")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "mesh_relay", "node_id": "relay-7"}


# --- Forwarding --------------------------------------------------------------


async def test_relay_appends_exactly_one_hop(keys) -> None:
    sink = RecordingSink()
    packet = _packet(keys)

    response = await _post_packet(_relay_app(keys, sink), packet)

    assert response.status_code == 202
    forwarded = PaymentPacket.model_validate_json(sink.last_body)
    assert forwarded.hop_count == 1
    assert forwarded.hops[0].node_id == "relay-1"


async def test_relay_does_not_alter_the_signed_region(keys) -> None:
    """The whole design rests on this: a hop changes nothing that is signed."""
    sink = RecordingSink()
    packet = _packet(keys)

    await _post_packet(_relay_app(keys, sink), packet)
    forwarded = PaymentPacket.model_validate_json(sink.last_body)

    assert forwarded.signing_bytes == packet.signing_bytes
    assert forwarded.signature == packet.signature
    assert forwarded.idempotency_key == packet.idempotency_key
    assert forwarded.envelope == packet.envelope
    assert forwarded.packet_id == packet.packet_id
    assert forwarded.sender_id == packet.sender_id
    forwarded.verify(keys.signing_public)


async def test_relay_forwards_to_another_relay_below_the_hop_target(keys) -> None:
    sink = RecordingSink()
    packet = _packet(keys)

    response = await _post_packet(_relay_app(keys, sink, hop_count_target=3), packet)

    assert sink.last_url == "http://relay-next/relay"
    assert response.json()["status"] == PacketStatus.RELAYED.value
    assert response.json()["forwarded_to"] == "http://relay-next/relay"


async def test_relay_forwards_to_the_bridge_once_the_hop_target_is_reached(keys) -> None:
    sink = RecordingSink()
    packet = _packet(keys)

    # Target of 1 means this single hop is enough to reach a connected node.
    response = await _post_packet(_relay_app(keys, sink, hop_count_target=1), packet)

    assert sink.last_url == "http://bridge-node/bridge"
    assert response.json()["status"] == PacketStatus.BRIDGED.value


async def test_relay_bridges_a_packet_that_already_has_hops(keys) -> None:
    sink = RecordingSink()
    packet = _packet(keys).append_hop(HopRecord(node_id="relay-0", received_at=datetime.now(UTC)))

    response = await _post_packet(_relay_app(keys, sink, hop_count_target=2), packet)

    assert sink.last_url == "http://bridge-node/bridge"
    forwarded = PaymentPacket.model_validate_json(sink.last_body)
    assert forwarded.hop_count == 2
    assert [hop.node_id for hop in forwarded.hops] == ["relay-0", "relay-1"]
    assert response.json()["hop_count"] == 2


async def test_relay_preserves_pre_existing_hops(keys) -> None:
    sink = RecordingSink()
    packet = _packet(keys)
    for index in range(3):
        packet = packet.append_hop(
            HopRecord(node_id=f"relay-{index}", received_at=datetime.now(UTC))
        )

    await _post_packet(_relay_app(keys, sink, hop_count_target=10), packet)
    forwarded = PaymentPacket.model_validate_json(sink.last_body)

    assert [hop.node_id for hop in forwarded.hops] == ["relay-0", "relay-1", "relay-2", "relay-1"]


async def test_relay_cannot_read_the_payment(keys) -> None:
    """A relay has no RSA private key, so the payload stays opaque to it."""
    sink = RecordingSink()
    packet = _packet(keys)

    await _post_packet(_relay_app(keys, sink), packet)

    assert b"device-bob" not in sink.last_body
    assert b"12500" not in sink.last_body


# --- Rejection ---------------------------------------------------------------


async def test_relay_rejects_a_tampered_packet_and_does_not_forward(keys) -> None:
    sink = RecordingSink()
    packet = _packet(keys)
    payload = json.loads(packet.model_dump_json())
    payload["sender_id"] = "device-attacker"

    transport = httpx.ASGITransport(app=_relay_app(keys, sink))
    async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
        response = await client.post("/relay", json=payload)

    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.INVALID_SIGNATURE.value
    assert sink.requests == [], "a packet that failed verification must not be forwarded"


async def test_relay_rejects_a_packet_signed_by_an_unknown_key(keys, other_keys) -> None:
    sink = RecordingSink()
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

    response = await _post_packet(_relay_app(keys, sink), forged)

    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.INVALID_SIGNATURE.value
    assert sink.requests == []


async def test_relay_rejects_malformed_json(keys) -> None:
    sink = RecordingSink()
    transport = httpx.ASGITransport(app=_relay_app(keys, sink))
    async with httpx.AsyncClient(transport=transport, base_url="http://relay") as client:
        response = await client.post(
            "/relay",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.MALFORMED_PAYLOAD.value
    assert sink.requests == []


async def test_relay_enforces_the_hop_limit(keys) -> None:
    """Loop protection: a packet cannot circulate forever."""
    sink = RecordingSink()
    packet = _packet(keys)
    for index in range(4):
        packet = packet.append_hop(
            HopRecord(node_id=f"relay-{index}", received_at=datetime.now(UTC))
        )

    response = await _post_packet(_relay_app(keys, sink, hop_count_target=99, hop_limit=4), packet)

    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.MALFORMED_PAYLOAD.value
    assert "hop limit" in response.json()["detail"]
    assert sink.requests == []


async def test_relay_reports_bad_gateway_when_the_next_node_is_unreachable(keys) -> None:
    app = create_app()
    config = RelayConfig(
        node_id="relay-1",
        next_relay_url="http://relay-next",
        bridge_url="http://bridge-node",
        hop_count_target=2,
        hop_limit=16,
    )

    async def _refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("next node is offline", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_refuse))
    app.dependency_overrides[get_sender_public_key] = lambda: keys.signing_public
    app.dependency_overrides[get_relay_config] = lambda: config
    app.dependency_overrides[get_http_client] = lambda: client

    response = await _post_packet(app, _packet(keys))

    assert response.status_code == 502
    assert response.json()["code"] == ErrorCode.INTERNAL_ERROR.value


async def test_relay_propagates_a_downstream_rejection(keys) -> None:
    """If the next node refuses the packet, this node must not report success."""
    sink = RecordingSink(status_code=400)
    response = await _post_packet(_relay_app(keys, sink), _packet(keys))

    assert response.status_code == 502
    assert response.json()["code"] == ErrorCode.INTERNAL_ERROR.value


# --- Routing decision --------------------------------------------------------


@pytest.mark.parametrize(
    ("hop_count", "target", "expected"),
    [
        (1, 2, "http://relay-next/relay"),
        (2, 2, "http://bridge-node/bridge"),
        (3, 2, "http://bridge-node/bridge"),
        (1, 1, "http://bridge-node/bridge"),
        (0, 0, "http://bridge-node/bridge"),
    ],
)
def test_next_destination_switches_to_the_bridge_at_the_target(
    hop_count: int, target: int, expected: str
) -> None:
    config = RelayConfig(
        node_id="relay-1",
        next_relay_url="http://relay-next",
        bridge_url="http://bridge-node",
        hop_count_target=target,
        hop_limit=16,
    )
    destination, _status = config.next_destination(hop_count)
    assert destination == expected


def test_next_destination_tolerates_trailing_slashes() -> None:
    config = RelayConfig(
        node_id="relay-1",
        next_relay_url="http://relay-next/",
        bridge_url="http://bridge-node/",
        hop_count_target=5,
        hop_limit=16,
    )
    assert config.next_destination(1)[0] == "http://relay-next/relay"
    assert config.next_destination(9)[0] == "http://bridge-node/bridge"
