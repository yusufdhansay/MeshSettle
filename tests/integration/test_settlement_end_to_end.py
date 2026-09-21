"""End-to-end: sender through mesh and bridge, over a real queue, to settled.

Everything real except the sockets between the mesh HTTP hops: a real
RabbitMQ broker, a real Redis, a real Postgres, the real consumer, and the
real processor. This is the test that shows the pieces actually compose, and
that duplicates delivered by the broker still settle exactly once.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import aio_pika
import httpx
import pytest
import redis.asyncio as aioredis

from services.bridge.app import create_app as create_bridge_app
from services.bridge.app import get_publisher
from services.bridge.app import get_sender_public_key as bridge_sender_key
from services.bridge.publisher import RabbitMQPublisher
from services.mesh_relay.app import RelayConfig, get_http_client, get_relay_config
from services.mesh_relay.app import create_app as create_relay_app
from services.mesh_relay.app import get_sender_public_key as relay_sender_key
from services.settlement.consumer import SettlementConsumer
from services.settlement.db import (
    count_settlements,
    create_all,
    create_engine,
    create_session_factory,
    drop_all,
    get_settlement,
)
from services.settlement.dedupe import DedupeStore
from services.settlement.processor import SettlementProcessor
from shared.models import PaymentInstruction, PaymentPacket
from tests.mesh_harness import mesh_client

pytestmark = pytest.mark.integration

AMQP_URL = os.environ.get("MESHSETTLE_TEST_AMQP_URL", "amqp://guest:guest@localhost:5673/")
PG_DSN = os.environ.get(
    "MESHSETTLE_TEST_PG_DSN",
    "postgresql+asyncpg://meshsettle:testonly-localdev@localhost:5433/meshsettle",
)
REDIS_URL = os.environ.get("MESHSETTLE_TEST_REDIS_URL", "redis://localhost:6380/0")

RELAY_URL = "http://relay-node"
BRIDGE_URL = "http://bridge-node"

SETTLE_TIMEOUT_SECONDS = 15.0


async def _reachable(check) -> bool:
    try:
        await check()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(autouse=True)
async def _require_infrastructure() -> None:
    async def amqp() -> None:
        connection = await asyncio.wait_for(aio_pika.connect_robust(AMQP_URL), timeout=5)
        await connection.close()

    async def postgres() -> None:
        engine = create_engine(PG_DSN)
        try:
            async with engine.connect():
                pass
        finally:
            await engine.dispose()

    async def redis() -> None:
        client = aioredis.from_url(REDIS_URL)
        try:
            await client.ping()
        finally:
            await client.aclose()

    if not await _reachable(amqp):
        pytest.skip(f"no AMQP broker reachable at {AMQP_URL}")
    if not await _reachable(postgres):
        pytest.skip(f"no Postgres reachable at {PG_DSN}")
    if not await _reachable(redis):
        pytest.skip(f"no Redis reachable at {REDIS_URL}")


@pytest.fixture
async def pipeline(keys) -> AsyncIterator[dict]:
    """A full pipeline: relay -> bridge -> real queue -> settlement consumer."""
    queue_name = f"meshsettle.e2e.{uuid4().hex[:12]}"

    engine = create_engine(PG_DSN)
    await drop_all(engine)
    await create_all(engine)
    session_factory = create_session_factory(engine)

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=False)
    await redis_client.flushdb()
    dedupe = DedupeStore(redis_client)

    processor = SettlementProcessor(
        session_factory=session_factory,
        dedupe=dedupe,
        sender_public_key=keys.signing_public,
        settlement_private_key=keys.rsa_private,
    )
    consumer = SettlementConsumer(processor, url=AMQP_URL, queue=queue_name)
    await consumer.start()

    publisher = RabbitMQPublisher(url=AMQP_URL, queue=queue_name)
    await publisher.connect()

    bridge_app = create_bridge_app()
    bridge_app.dependency_overrides[bridge_sender_key] = lambda: keys.signing_public
    bridge_app.dependency_overrides[get_publisher] = lambda: publisher

    relay_app = create_relay_app()
    relay_app.dependency_overrides[relay_sender_key] = lambda: keys.signing_public
    relay_app.dependency_overrides[get_relay_config] = lambda: RelayConfig(
        node_id="relay-1",
        next_relay_url=RELAY_URL,
        bridge_url=BRIDGE_URL,
        hop_count_target=2,
        hop_limit=32,
    )
    forwarding_client = mesh_client({RELAY_URL: relay_app, BRIDGE_URL: bridge_app})
    relay_app.dependency_overrides[get_http_client] = lambda: forwarding_client

    try:
        yield {
            "relay_app": relay_app,
            "processor": processor,
            "session_factory": session_factory,
            "dedupe": dedupe,
            "queue_name": queue_name,
        }
    finally:
        await forwarding_client.aclose()
        await publisher.close()
        await consumer.stop()
        await redis_client.flushdb()
        await redis_client.aclose()
        await drop_all(engine)
        await engine.dispose()


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


async def _send_into_mesh(relay_app, packet: PaymentPacket) -> httpx.Response:
    transport = httpx.ASGITransport(app=relay_app)
    async with httpx.AsyncClient(transport=transport, base_url=RELAY_URL) as client:
        return await client.post(
            "/relay",
            content=packet.model_dump_json(),
            headers={"Content-Type": "application/json"},
        )


async def _wait_for_settlements(session_factory, expected: int) -> int:
    """Poll until the expected number of settlements exist, or time out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_TIMEOUT_SECONDS
    observed = 0
    while loop.time() < deadline:
        async with session_factory() as session:
            observed = await count_settlements(session)
        if observed >= expected:
            return observed
        await asyncio.sleep(0.05)
    return observed


# --- The happy path ----------------------------------------------------------


async def test_packet_travels_the_full_pipeline_and_settles(pipeline, keys) -> None:
    packet = _packet(keys, amount_minor=456_78)

    response = await _send_into_mesh(pipeline["relay_app"], packet)
    assert response.status_code == 202

    assert await _wait_for_settlements(pipeline["session_factory"], 1) == 1

    async with pipeline["session_factory"]() as session:
        row = await get_settlement(session, packet.idempotency_key)

    assert row is not None
    assert row.amount_minor == 456_78
    assert row.payee_id == "device-bob"
    assert row.packet_id == packet.packet_id
    # Two mesh hops were recorded on the way.
    assert row.hop_count == 2
    # The settled timestamp is after the packet was created: the offline window.
    assert row.settled_at >= row.packet_created_at


async def test_several_distinct_payments_all_settle(pipeline, keys) -> None:
    packets = [_packet(keys, amount_minor=1_000 + index) for index in range(10)]

    for packet in packets:
        response = await _send_into_mesh(pipeline["relay_app"], packet)
        assert response.status_code == 202

    assert await _wait_for_settlements(pipeline["session_factory"], 10) == 10


# --- Duplicates over the real broker ----------------------------------------


async def test_the_same_packet_sent_many_times_settles_once(pipeline, keys) -> None:
    """The broker delivers every copy; settlement collapses them to one."""
    packet = _packet(keys)

    for _ in range(20):
        response = await _send_into_mesh(pipeline["relay_app"], packet)
        assert response.status_code == 202

    assert await _wait_for_settlements(pipeline["session_factory"], 1) == 1

    # Give any stragglers time to be consumed, then confirm still exactly one.
    await asyncio.sleep(1.0)
    async with pipeline["session_factory"]() as session:
        assert await count_settlements(session) == 1

    metrics = pipeline["processor"].metrics
    assert metrics.settled == 1
    assert metrics.duplicates == 19


async def test_concurrent_submissions_of_one_packet_settle_once(pipeline, keys) -> None:
    packet = _packet(keys)

    responses = await asyncio.gather(
        *(_send_into_mesh(pipeline["relay_app"], packet) for _ in range(25))
    )
    assert all(response.status_code == 202 for response in responses)

    assert await _wait_for_settlements(pipeline["session_factory"], 1) == 1
    await asyncio.sleep(1.0)
    async with pipeline["session_factory"]() as session:
        assert await count_settlements(session) == 1


# --- Rejection through the pipeline -----------------------------------------


async def test_forged_packet_is_stopped_before_the_queue(pipeline, keys, other_keys) -> None:
    """The relay rejects it, so it never reaches the broker or settlement."""
    forged = _packet(other_keys)

    response = await _send_into_mesh(pipeline["relay_app"], forged)
    assert response.status_code == 400

    await asyncio.sleep(0.5)
    async with pipeline["session_factory"]() as session:
        assert await count_settlements(session) == 0
    assert pipeline["processor"].metrics.settled == 0
