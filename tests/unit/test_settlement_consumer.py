"""Unit tests for the consumer's ack/nack decisions.

The consumer's only real responsibility is translating an outcome into a
delivery decision, and getting that wrong loses or loops payments:

* ack a settled message, so it is not redelivered
* ack a permanently rejected message, so it does not loop forever
* nack-requeue a transient failure, so the payment is not lost
* nack-requeue if the processor itself raises, because an unexpected crash
  must never silently drop a payment

These use a fake message rather than a broker, because what is under test is
the branching logic, not AMQP. The real broker path is covered in
``tests/integration/test_settlement_end_to_end.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from services.settlement.consumer import SettlementConsumer
from services.settlement.processor import SettlementOutcome
from shared.models import ErrorCode, PacketStatus


@dataclass
class FakeMessage:
    """Records how the consumer resolved the delivery."""

    body: bytes = b"{}"
    acked: bool = False
    nacked: bool = False
    requeued: bool | None = None

    async def ack(self) -> None:
        self.acked = True

    async def nack(self, requeue: bool = True) -> None:
        self.nacked = True
        self.requeued = requeue


@dataclass
class StubProcessor:
    """Returns a scripted outcome, or raises."""

    outcome: SettlementOutcome | None = None
    raises: bool = False
    calls: list[bytes] = field(default_factory=list)

    async def process(self, body: bytes) -> SettlementOutcome:
        self.calls.append(body)
        if self.raises:
            raise RuntimeError("processor blew up")
        assert self.outcome is not None
        return self.outcome


def _settled() -> SettlementOutcome:
    return SettlementOutcome(
        status=PacketStatus.SETTLED,
        detail="settled",
        idempotency_key="settle:" + "a" * 64,
        packet_id=uuid4(),
        settlement_id=1,
    )


def _rejected(code: ErrorCode, *, retryable: bool = False) -> SettlementOutcome:
    return SettlementOutcome(
        status=PacketStatus.REJECTED,
        detail=code.value,
        code=code,
        idempotency_key="settle:" + "b" * 64,
        packet_id=uuid4(),
        retryable=retryable,
    )


def _consumer(processor: StubProcessor) -> SettlementConsumer:
    return SettlementConsumer(processor)  # type: ignore[arg-type]


# --- Ack on completion -------------------------------------------------------


async def test_settled_message_is_acked() -> None:
    processor = StubProcessor(outcome=_settled())
    message = FakeMessage()

    await _consumer(processor)._handle_message(message)  # type: ignore[arg-type]

    assert message.acked is True
    assert message.nacked is False


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.DUPLICATE_PACKET,
        ErrorCode.INVALID_SIGNATURE,
        ErrorCode.MALFORMED_PAYLOAD,
        ErrorCode.DECRYPTION_FAILED,
    ],
)
async def test_permanently_rejected_message_is_acked_not_requeued(code: ErrorCode) -> None:
    """An invalid packet will never become valid; requeueing would loop forever."""
    processor = StubProcessor(outcome=_rejected(code))
    message = FakeMessage()

    await _consumer(processor)._handle_message(message)  # type: ignore[arg-type]

    assert message.acked is True
    assert message.nacked is False


# --- Requeue on transient failure -------------------------------------------


async def test_retryable_outcome_is_requeued() -> None:
    """A transient infrastructure failure must not consume the payment."""
    processor = StubProcessor(outcome=_rejected(ErrorCode.INTERNAL_ERROR, retryable=True))
    message = FakeMessage()

    await _consumer(processor)._handle_message(message)  # type: ignore[arg-type]

    assert message.nacked is True
    assert message.requeued is True
    assert message.acked is False


async def test_processor_crash_requeues_rather_than_dropping() -> None:
    """If the processor raises, the message goes back on the queue."""
    processor = StubProcessor(raises=True)
    message = FakeMessage()

    await _consumer(processor)._handle_message(message)  # type: ignore[arg-type]

    assert message.nacked is True
    assert message.requeued is True
    assert message.acked is False


# --- Plumbing ----------------------------------------------------------------


async def test_message_body_is_passed_through_unchanged() -> None:
    processor = StubProcessor(outcome=_settled())
    body = b'{"packet_id": "abc"}'
    message = FakeMessage(body=body)

    await _consumer(processor)._handle_message(message)  # type: ignore[arg-type]

    assert processor.calls == [body]


async def test_outcome_callback_is_invoked() -> None:
    """The callback exists so the demo UI can stream progress."""
    processor = StubProcessor(outcome=_settled())
    seen: list[SettlementOutcome] = []

    async def record(outcome: SettlementOutcome) -> None:
        seen.append(outcome)

    consumer = SettlementConsumer(processor, on_outcome=record)  # type: ignore[arg-type]
    await consumer._handle_message(FakeMessage())  # type: ignore[arg-type]

    assert len(seen) == 1
    assert seen[0].settled is True


async def test_queue_depth_is_none_when_disconnected() -> None:
    consumer = _consumer(StubProcessor(outcome=_settled()))
    assert await consumer.queue_depth() is None
