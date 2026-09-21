"""Phase 3 integration test: a packet survives N mesh hops unmodified.

This wires real relay and bridge apps together and sends a packet through the
chain. Each hop serializes the packet to JSON, sends it as an HTTP body, and
the next node parses and re-validates it, so the round trip is genuine. Only
the socket is replaced (see ``tests/mesh_harness``).

The property under test is the one the whole architecture rests on: after any
number of hops, the signed region is byte-identical and the idempotency key is
unchanged, so settlement can dedupe two copies that travelled different paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from services.bridge.app import create_app as create_bridge_app
from services.bridge.app import get_publisher
from services.bridge.app import get_sender_public_key as bridge_sender_key
from services.bridge.publisher import InMemoryPublisher
from services.mesh_relay.app import RelayConfig, get_http_client, get_relay_config
from services.mesh_relay.app import create_app as create_relay_app
from services.mesh_relay.app import get_sender_public_key as relay_sender_key
from shared.models import PacketStatus, PaymentInstruction, PaymentPacket
from tests.mesh_harness import mesh_client

RELAY_URL = "http://relay-node"
BRIDGE_URL = "http://bridge-node"


def _packet(keys, amount_minor: int = 125_00) -> PaymentPacket:
    instruction = PaymentInstruction(
        packet_id=uuid4(),
        payer_id="device-alice",
        payee_id="device-bob",
        amount_minor=amount_minor,
        currency="INR",
        created_at=datetime.now(UTC),
    )
    return PaymentPacket.create(
        instruction=instruction,
        sender_id="device-alice",
        signing_key=keys.signing_private,
        recipient_public_key=keys.rsa_public,
    )


def _build_mesh(keys, *, hop_count_target: int):
    """Build a relay that loops back to itself until the hop target, then bridges.

    Returns the relay app, the bridge's publisher, and a client bound to the
    relay entrypoint.
    """
    publisher = InMemoryPublisher()

    bridge_app = create_bridge_app()
    bridge_app.dependency_overrides[bridge_sender_key] = lambda: keys.signing_public
    bridge_app.dependency_overrides[get_publisher] = lambda: publisher

    relay_app = create_relay_app()
    config = RelayConfig(
        node_id="relay-1",
        next_relay_url=RELAY_URL,
        bridge_url=BRIDGE_URL,
        hop_count_target=hop_count_target,
        hop_limit=32,
    )
    relay_app.dependency_overrides[relay_sender_key] = lambda: keys.signing_public
    relay_app.dependency_overrides[get_relay_config] = lambda: config

    # The relay forwards to absolute URLs; route them to the in-process apps.
    forwarding_client = mesh_client({RELAY_URL: relay_app, BRIDGE_URL: bridge_app})
    relay_app.dependency_overrides[get_http_client] = lambda: forwarding_client

    return relay_app, publisher, forwarding_client


async def _send_into_mesh(relay_app, packet: PaymentPacket) -> httpx.Response:
    transport = httpx.ASGITransport(app=relay_app)
    async with httpx.AsyncClient(transport=transport, base_url=RELAY_URL) as client:
        return await client.post(
            "/relay",
            content=packet.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )


# --- The required property ---------------------------------------------------


@pytest.mark.parametrize("hops", [1, 2, 3, 5, 8])
async def test_packet_survives_n_hops_unmodified(keys, hops: int) -> None:
    """After N hops the signed region is byte-identical to what was created."""
    relay_app, publisher, forwarding_client = _build_mesh(keys, hop_count_target=hops)
    original = _packet(keys)

    try:
        response = await _send_into_mesh(relay_app, original)
    finally:
        await forwarding_client.aclose()

    assert response.status_code == 202
    assert len(publisher.messages) == 1, "exactly one packet should reach the queue"

    queued = PaymentPacket.model_validate_json(publisher.messages[0][0])

    # The signed region is untouched.
    assert queued.signing_bytes == original.signing_bytes
    assert queued.signature == original.signature
    assert queued.packet_id == original.packet_id
    assert queued.sender_id == original.sender_id
    assert queued.created_at == original.created_at
    assert queued.envelope == original.envelope

    # The dedupe key is therefore stable end to end.
    assert queued.idempotency_key == original.idempotency_key

    # It still verifies against the sender's key after the whole journey.
    queued.verify(keys.signing_public)

    # And the hop trail records every node it passed through.
    assert queued.hop_count == hops
    assert all(hop.node_id == "relay-1" for hop in queued.hops)


async def test_payment_is_readable_only_at_the_end_of_the_journey(keys) -> None:
    """No intermediate node could read the instruction; settlement's key can."""
    relay_app, publisher, forwarding_client = _build_mesh(keys, hop_count_target=3)
    original = _packet(keys, amount_minor=987_65)

    try:
        await _send_into_mesh(relay_app, original)
    finally:
        await forwarding_client.aclose()

    queued_body = publisher.messages[0][0]
    assert b"device-bob" not in queued_body
    assert b"98765" not in queued_body

    queued = PaymentPacket.model_validate_json(queued_body)
    instruction = queued.open_instruction(keys.rsa_private)
    assert instruction.amount_minor == 987_65
    assert instruction.payee_id == "device-bob"


async def test_two_copies_via_different_hop_counts_share_an_idempotency_key(keys) -> None:
    """The dedupe guarantee: same instruction, different paths, one key.

    This is what lets settlement collapse duplicates that arrived through
    separate mesh routes.
    """
    original = _packet(keys)
    keys_seen = []

    for hops in (1, 4):
        relay_app, publisher, forwarding_client = _build_mesh(keys, hop_count_target=hops)
        try:
            await _send_into_mesh(relay_app, original)
        finally:
            await forwarding_client.aclose()

        queued = PaymentPacket.model_validate_json(publisher.messages[0][0])
        assert queued.hop_count == hops
        keys_seen.append(queued.idempotency_key)

    assert keys_seen[0] == keys_seen[1] == original.idempotency_key


async def test_bridge_reports_bridged_status_through_the_chain(keys) -> None:
    relay_app, publisher, forwarding_client = _build_mesh(keys, hop_count_target=1)

    try:
        response = await _send_into_mesh(relay_app, _packet(keys))
    finally:
        await forwarding_client.aclose()

    body = response.json()
    assert body["status"] == PacketStatus.BRIDGED.value
    assert body["forwarded_to"] == f"{BRIDGE_URL}/bridge"
    assert body["hop_count"] == 1
    assert len(publisher.messages) == 1


async def test_a_forged_packet_never_reaches_the_queue(keys, other_keys) -> None:
    """Verification at the first hop stops a forgery before it consumes the mesh."""
    relay_app, publisher, forwarding_client = _build_mesh(keys, hop_count_target=3)
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

    try:
        response = await _send_into_mesh(relay_app, forged)
    finally:
        await forwarding_client.aclose()

    assert response.status_code == 400
    assert publisher.messages == []
