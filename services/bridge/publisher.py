"""RabbitMQ publishing for the bridge node.

Kept separate from the HTTP layer so the bridge's request handling can be
tested against an in-memory publisher, and so the broker wiring has one
obvious home.

Durability choices, and why:

* The queue is declared ``durable`` and messages are published
  ``PERSISTENT``. A packet that reached the bridge represents a payment
  someone is waiting on; losing it on a broker restart would turn a real
  transaction into a silent no-op.
* Publishing uses a publisher-confirms channel, so ``publish`` only returns
  once the broker has acknowledged the message. Without confirms, a
  successful ``publish`` call means "handed to a socket", not "the broker
  has it", and the bridge would report success for messages it lost.
* ``message_id`` is set to the packet's idempotency key. That gives the
  broker and any operator a stable handle on the message, and makes
  duplicates visible in broker tooling.
"""

from __future__ import annotations

from typing import Protocol

import aio_pika
from aio_pika.abc import AbstractRobustChannel, AbstractRobustConnection

from shared.config import settings
from shared.logging import get_logger

logger = get_logger("bridge.publisher")


class Publisher(Protocol):
    """What the bridge needs from a queue. Implemented for real and for tests."""

    async def publish(self, body: bytes, *, message_id: str) -> None:
        """Hand a message to the queue, returning once it is safely accepted."""
        ...

    @property
    def queue_name(self) -> str:
        """Queue messages are published to."""
        ...


class PublishError(RuntimeError):
    """The message could not be confirmed by the broker."""


class RabbitMQPublisher:
    """Publishes packets to a durable RabbitMQ queue with publisher confirms."""

    def __init__(
        self,
        *,
        url: str | None = None,
        queue: str | None = None,
    ) -> None:
        self._url = url or settings.rabbitmq_url
        self._queue = queue or settings.settlement_queue
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractRobustChannel | None = None

    @property
    def queue_name(self) -> str:
        return self._queue

    async def connect(self) -> None:
        """Open a robust connection and declare the durable queue.

        ``connect_robust`` reconnects on its own if the broker restarts,
        which matters because the bridge is the component that comes and
        goes with connectivity.
        """
        self._connection = await aio_pika.connect_robust(self._url)
        channel = await self._connection.channel(publisher_confirms=True)
        await channel.declare_queue(self._queue, durable=True)
        self._channel = channel  # type: ignore[assignment]
        logger.info("bridge.publisher_connected", queue=self._queue)

    async def close(self) -> None:
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._connection = None
        self._channel = None
        logger.info("bridge.publisher_closed", queue=self._queue)

    async def publish(self, body: bytes, *, message_id: str) -> None:
        """Publish one packet, waiting for the broker's confirmation.

        Raises:
            PublishError: if the publisher is not connected, or the broker
                did not confirm the message.
        """
        if self._channel is None:
            raise PublishError("publisher is not connected")

        message = aio_pika.Message(
            body=body,
            message_id=message_id,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        try:
            await self._channel.default_exchange.publish(
                message,
                routing_key=self._queue,
            )
        except Exception as exc:  # noqa: BLE001 - normalized for the caller
            raise PublishError("broker did not confirm the message") from exc


class InMemoryPublisher:
    """A publisher that records messages instead of sending them.

    Used by tests so the bridge's HTTP behaviour can be verified without a
    broker. Deliberately lives in the source tree rather than the test tree
    because the demo/compose flow can also use it to run without RabbitMQ.
    """

    def __init__(self, queue: str | None = None) -> None:
        self._queue = queue or settings.settlement_queue
        self.messages: list[tuple[bytes, str]] = []

    @property
    def queue_name(self) -> str:
        return self._queue

    async def publish(self, body: bytes, *, message_id: str) -> None:
        self.messages.append((body, message_id))
