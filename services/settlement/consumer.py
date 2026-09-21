"""RabbitMQ consumer for the settlement service.

The consumer is intentionally thin: it moves bytes from the queue into the
processor and translates the outcome into an ack or a nack. All the
correctness logic lives in :mod:`services.settlement.processor`.

Two delivery decisions matter:

``prefetch_count``
    Bounds how many unacknowledged messages one consumer holds. Without it a
    single consumer would greedily buffer the whole queue, which defeats
    horizontal scaling and makes the HPA in Phase 7 pointless.

Ack only after commit
    A message is acknowledged only once the Postgres transaction has
    committed. If the consumer dies mid-settlement, the broker redelivers,
    the expired Redis claim is winnable again, and the packet still settles
    exactly once. Acking on receipt would lose payments on a crash.

A packet that is rejected for being invalid or duplicate is acked, not
requeued: it will never become valid, so requeueing would spin forever.
Only transient infrastructure failures are nacked with ``requeue=True``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import aio_pika
from aio_pika.abc import AbstractIncomingMessage, AbstractRobustConnection

from services.settlement.processor import SettlementOutcome, SettlementProcessor
from shared.config import settings
from shared.logging import get_logger

logger = get_logger("settlement.consumer")

#: Unacknowledged messages a single consumer will hold at once.
DEFAULT_PREFETCH = 32


class SettlementConsumer:
    """Consumes packets from RabbitMQ and settles them."""

    def __init__(
        self,
        processor: SettlementProcessor,
        *,
        url: str | None = None,
        queue: str | None = None,
        prefetch_count: int = DEFAULT_PREFETCH,
        on_outcome: Callable[[SettlementOutcome], Awaitable[None]] | None = None,
    ) -> None:
        self._processor = processor
        self._url = url or settings.rabbitmq_url
        self._queue_name = queue or settings.settlement_queue
        self._prefetch_count = prefetch_count
        self._on_outcome = on_outcome
        self._connection: AbstractRobustConnection | None = None
        self._consumer_tag: str | None = None
        self._queue: aio_pika.abc.AbstractQueue | None = None
        self._stopped = asyncio.Event()

    @property
    def queue_name(self) -> str:
        return self._queue_name

    async def start(self) -> None:
        """Connect and begin consuming in the background."""
        self._connection = await aio_pika.connect_robust(self._url)
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=self._prefetch_count)
        self._queue = await channel.declare_queue(self._queue_name, durable=True)
        self._consumer_tag = await self._queue.consume(self._handle_message)
        logger.info(
            "settlement.consumer_started",
            queue=self._queue_name,
            prefetch_count=self._prefetch_count,
        )

    async def stop(self) -> None:
        """Stop consuming and close the connection."""
        if self._queue is not None and self._consumer_tag is not None:
            await self._queue.cancel(self._consumer_tag)
            self._consumer_tag = None
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._connection = None
        self._queue = None
        self._stopped.set()
        logger.info("settlement.consumer_stopped", queue=self._queue_name)

    async def _handle_message(self, message: AbstractIncomingMessage) -> None:
        """Settle one message, then ack or nack based on the outcome."""
        try:
            outcome = await self._processor.process(message.body)
        except Exception:
            # The processor is meant to convert failures into outcomes, so
            # reaching here is a bug. Requeue rather than silently dropping a
            # payment, and make the bug visible.
            logger.exception("settlement.handler_crashed", queue=self._queue_name)
            await message.nack(requeue=True)
            return

        if outcome.retryable:
            # Transient failure: give the broker the message back.
            await message.nack(requeue=True)
        else:
            # Settled, or permanently rejected. Either way this delivery is
            # finished; requeueing an invalid packet would loop forever.
            await message.ack()

        if self._on_outcome is not None:
            await self._on_outcome(outcome)

    async def wait_closed(self) -> None:
        """Block until :meth:`stop` has run. Used by the service entrypoint."""
        await self._stopped.wait()

    async def queue_depth(self) -> int | None:
        """Messages currently waiting on the queue.

        Used by the health/metrics endpoint and available as the custom
        scaling signal referenced by the Phase 7 HPA. Returns ``None`` if the
        consumer is not connected.
        """
        if self._connection is None or self._connection.is_closed:
            return None
        channel = await self._connection.channel()
        try:
            declared = await channel.declare_queue(self._queue_name, durable=True, passive=True)
            return declared.declaration_result.message_count
        finally:
            await channel.close()
