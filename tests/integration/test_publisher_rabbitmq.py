"""Integration tests for the real RabbitMQ publisher.

The rest of the suite exercises the bridge against an in-memory publisher,
which proves the HTTP behaviour but says nothing about whether the broker
wiring is correct. These tests talk to an actual broker and assert the
properties the settlement guarantee depends on:

* the queue is durable and the message is persistent, so a broker restart
  does not vaporize a payment
* the bytes the consumer receives are byte-identical to what was published
* ``message_id`` carries the idempotency key

Skipped automatically when no broker is reachable, so the default `pytest`
run does not require Docker. Point at a broker with ``MESHSETTLE_TEST_AMQP_URL``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import aio_pika
import pytest

from services.bridge.publisher import PublishError, RabbitMQPublisher
from shared.models import PaymentInstruction, PaymentPacket

pytestmark = pytest.mark.integration

AMQP_URL = os.environ.get("MESHSETTLE_TEST_AMQP_URL", "amqp://guest:guest@localhost:5673/")
CONNECT_TIMEOUT_SECONDS = 5.0


async def _broker_reachable() -> bool:
    try:
        connection = await asyncio.wait_for(
            aio_pika.connect_robust(AMQP_URL),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return False
    await connection.close()
    return True


@pytest.fixture(autouse=True)
async def _require_broker() -> None:
    if not await _broker_reachable():
        pytest.skip(f"no AMQP broker reachable at {AMQP_URL}")


@pytest.fixture
async def queue_name() -> str:
    """A unique queue per test, cleaned up afterwards."""
    name = f"meshsettle.test.{uuid4().hex[:12]}"
    yield name

    connection = await aio_pika.connect_robust(AMQP_URL)
    channel = await connection.channel()
    await channel.queue_delete(name)
    await connection.close()


def _packet(keys) -> PaymentPacket:
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
        sender_id="device-alice",
        signing_key=keys.signing_private,
        recipient_public_key=keys.rsa_public,
    )


CONSUME_WAIT_SECONDS = 10.0


async def _consume_one(queue_name: str) -> aio_pika.abc.AbstractIncomingMessage:
    """Pull exactly one message off the queue, polling until it shows up."""
    connection = await aio_pika.connect_robust(AMQP_URL)
    try:
        channel = await connection.channel()
        queue = await channel.declare_queue(queue_name, durable=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONSUME_WAIT_SECONDS
        while loop.time() < deadline:
            message = await queue.get(fail=False)
            if message is not None:
                await message.ack()
                return message
            await asyncio.sleep(0.05)
        raise AssertionError(f"no message arrived on {queue_name} within {CONSUME_WAIT_SECONDS}s")
    finally:
        await connection.close()


# --- Publishing against a real broker ---------------------------------------


async def test_publish_delivers_the_exact_bytes(keys, queue_name: str) -> None:
    packet = _packet(keys)
    body = packet.model_dump_json().encode("utf-8")

    publisher = RabbitMQPublisher(url=AMQP_URL, queue=queue_name)
    await publisher.connect()
    try:
        await publisher.publish(body, message_id=packet.idempotency_key)
    finally:
        await publisher.close()

    message = await _consume_one(queue_name)
    assert message.body == body

    # And it still verifies after the broker round trip.
    delivered = PaymentPacket.model_validate_json(message.body)
    delivered.verify(keys.signing_public)
    assert delivered.idempotency_key == packet.idempotency_key


async def test_published_message_is_persistent_and_carries_the_idempotency_key(
    keys, queue_name: str
) -> None:
    packet = _packet(keys)

    publisher = RabbitMQPublisher(url=AMQP_URL, queue=queue_name)
    await publisher.connect()
    try:
        await publisher.publish(
            packet.model_dump_json().encode("utf-8"),
            message_id=packet.idempotency_key,
        )
    finally:
        await publisher.close()

    message = await _consume_one(queue_name)
    assert message.message_id == packet.idempotency_key
    assert message.content_type == "application/json"
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT.value


async def test_duplicates_are_all_delivered(keys, queue_name: str) -> None:
    """The broker does not dedupe. Settlement has to, which is the point."""
    packet = _packet(keys)
    body = packet.model_dump_json().encode("utf-8")

    publisher = RabbitMQPublisher(url=AMQP_URL, queue=queue_name)
    await publisher.connect()
    try:
        for _ in range(5):
            await publisher.publish(body, message_id=packet.idempotency_key)
    finally:
        await publisher.close()

    seen = [await _consume_one(queue_name) for _ in range(5)]
    assert len(seen) == 5
    assert all(message.body == body for message in seen)
    assert {message.message_id for message in seen} == {packet.idempotency_key}


async def test_queue_survives_a_reconnect(keys, queue_name: str) -> None:
    """A durable queue keeps messages across publisher lifecycles."""
    packet = _packet(keys)
    body = packet.model_dump_json().encode("utf-8")

    first = RabbitMQPublisher(url=AMQP_URL, queue=queue_name)
    await first.connect()
    await first.publish(body, message_id=packet.idempotency_key)
    await first.close()

    # A completely separate publisher instance, as after a bridge restart.
    second = RabbitMQPublisher(url=AMQP_URL, queue=queue_name)
    await second.connect()
    await second.close()

    message = await _consume_one(queue_name)
    assert message.body == body


async def test_publish_without_connect_raises(keys, queue_name: str) -> None:
    """Never silently drop a packet because the publisher was not connected."""
    publisher = RabbitMQPublisher(url=AMQP_URL, queue=queue_name)
    with pytest.raises(PublishError, match="not connected"):
        await publisher.publish(b"{}", message_id="settle:deadbeef")


async def test_close_is_idempotent(keys, queue_name: str) -> None:
    publisher = RabbitMQPublisher(url=AMQP_URL, queue=queue_name)
    await publisher.connect()
    await publisher.close()
    await publisher.close()  # must not raise
