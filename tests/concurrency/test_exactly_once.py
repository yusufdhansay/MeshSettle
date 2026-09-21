"""Exactly-once settlement under duplicate and concurrent delivery.

This is the test the project exists to pass. It runs against real Redis and
real Postgres and fires genuinely concurrent coroutines at the real processor,
so what it verifies is the actual interaction of ``SET NX`` and the unique
constraint, not a model of them.

The headline case required by TASK.md: fire 50 duplicate/simultaneous packets,
assert exactly one settles.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from services.settlement.db import count_settlements
from services.settlement.dedupe import SETTLED_MARKER, ClaimOutcome, DedupeStore
from services.settlement.processor import SettlementProcessor
from shared.models import ErrorCode, HopRecord, PacketStatus, PaymentPacket
from tests.concurrency.conftest import build_packet

pytestmark = pytest.mark.integration

#: Duplicate count for the headline test. TASK.md says start with 50.
DUPLICATE_COUNT = 50


async def _settle_all(processor: SettlementProcessor, bodies: list[bytes]) -> list[object]:
    """Fire every body at the processor concurrently."""
    return await asyncio.gather(*(processor.process(body) for body in bodies))


# --- The headline guarantee --------------------------------------------------


async def test_fifty_simultaneous_duplicates_settle_exactly_once(
    processor: SettlementProcessor, session_factory, packet: PaymentPacket
) -> None:
    body = packet.model_dump_json().encode("utf-8")
    outcomes = await _settle_all(processor, [body] * DUPLICATE_COUNT)

    settled = [outcome for outcome in outcomes if outcome.settled]
    rejected = [outcome for outcome in outcomes if not outcome.settled]

    assert len(settled) == 1, f"expected exactly 1 settlement, got {len(settled)}"
    assert len(rejected) == DUPLICATE_COUNT - 1
    assert all(outcome.code is ErrorCode.DUPLICATE_PACKET for outcome in rejected)

    # The database is the source of truth, so assert there too.
    async with session_factory() as session:
        assert await count_settlements(session, packet.idempotency_key) == 1
        assert await count_settlements(session) == 1

    assert processor.metrics.settled == 1
    assert processor.metrics.duplicates == DUPLICATE_COUNT - 1


async def test_sequential_redeliveries_settle_exactly_once(
    processor: SettlementProcessor, session_factory, packet: PaymentPacket
) -> None:
    """The realistic duplicate case: the broker redelivers the same message."""
    body = packet.model_dump_json().encode("utf-8")

    first = await processor.process(body)
    assert first.settled

    for _ in range(10):
        repeat = await processor.process(body)
        assert not repeat.settled
        assert repeat.code is ErrorCode.DUPLICATE_PACKET

    async with session_factory() as session:
        assert await count_settlements(session) == 1


async def test_duplicates_arriving_via_different_hop_paths_settle_once(
    processor: SettlementProcessor, session_factory, packet: PaymentPacket
) -> None:
    """Two copies of one instruction that travelled different mesh routes.

    This is the scenario the architecture is designed around: the same packet
    reaches the bridge twice, by different paths, so the hop trails differ but
    the signed region does not.
    """
    short_path = packet.append_hop(HopRecord(node_id="relay-1", received_at=datetime.now(UTC)))
    long_path = packet
    for index in range(5):
        long_path = long_path.append_hop(
            HopRecord(node_id=f"relay-{index}", received_at=datetime.now(UTC))
        )

    assert short_path.hop_count != long_path.hop_count
    assert short_path.idempotency_key == long_path.idempotency_key

    outcomes = await _settle_all(
        processor,
        [
            short_path.model_dump_json().encode("utf-8"),
            long_path.model_dump_json().encode("utf-8"),
        ],
    )

    assert sum(1 for outcome in outcomes if outcome.settled) == 1
    async with session_factory() as session:
        assert await count_settlements(session) == 1


@pytest.mark.parametrize("duplicate_count", [2, 10, 100, 250])
async def test_exactly_once_holds_at_several_concurrency_levels(
    processor: SettlementProcessor, session_factory, keys, duplicate_count: int
) -> None:
    """The guarantee should not be a function of how hard you push it."""
    packet = build_packet(keys)
    body = packet.model_dump_json().encode("utf-8")

    outcomes = await _settle_all(processor, [body] * duplicate_count)

    assert sum(1 for outcome in outcomes if outcome.settled) == 1
    async with session_factory() as session:
        assert await count_settlements(session) == 1


# --- Distinct packets must all settle ---------------------------------------


async def test_distinct_packets_all_settle(
    processor: SettlementProcessor, session_factory, keys
) -> None:
    """Deduplication must not be over-eager: different payments all settle."""
    packets = [build_packet(keys, amount_minor=100 + index) for index in range(25)]
    bodies = [packet.model_dump_json().encode("utf-8") for packet in packets]

    outcomes = await _settle_all(processor, bodies)

    assert all(outcome.settled for outcome in outcomes)
    async with session_factory() as session:
        assert await count_settlements(session) == 25


async def test_interleaved_distinct_and_duplicate_packets(
    processor: SettlementProcessor, session_factory, keys
) -> None:
    """A realistic mixed burst: several payments, each arriving several times."""
    packets = [build_packet(keys, amount_minor=500 + index) for index in range(10)]
    bodies: list[bytes] = []
    for packet in packets:
        bodies.extend([packet.model_dump_json().encode("utf-8")] * 5)

    outcomes = await _settle_all(processor, bodies)

    assert sum(1 for outcome in outcomes if outcome.settled) == 10
    async with session_factory() as session:
        assert await count_settlements(session) == 10
        for packet in packets:
            assert await count_settlements(session, packet.idempotency_key) == 1


# --- The Redis layer behaves as claimed -------------------------------------


async def test_duplicate_is_rejected_before_postgres_is_touched(
    processor: SettlementProcessor, session_factory, packet: PaymentPacket
) -> None:
    """A duplicate must not open a database transaction at all.

    Verified by settling once, then breaking the database and replaying the
    packet: if the duplicate path touched Postgres it would fail loudly
    instead of being cleanly rejected by Redis.
    """
    body = packet.model_dump_json().encode("utf-8")
    assert (await processor.process(body)).settled

    # Swap in a session factory that raises if anyone tries to use it.
    class ExplodingSessionFactory:
        def __call__(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("duplicate path must not touch Postgres")

    processor._session_factory = ExplodingSessionFactory()  # type: ignore[assignment]

    repeat = await processor.process(body)
    assert not repeat.settled
    assert repeat.code is ErrorCode.DUPLICATE_PACKET


async def test_only_one_claim_wins(dedupe: DedupeStore) -> None:
    """The atomic primitive itself: N racers, one winner."""
    key = "settle:" + "a" * 64
    outcomes = await asyncio.gather(*(dedupe.claim(key) for _ in range(100)))

    assert sum(1 for outcome in outcomes if outcome is ClaimOutcome.CLAIMED) == 1
    assert all(
        outcome is ClaimOutcome.IN_FLIGHT
        for outcome in outcomes
        if outcome is not ClaimOutcome.CLAIMED
    )


async def test_settled_marker_is_written_after_settlement(
    processor: SettlementProcessor, dedupe: DedupeStore, packet: PaymentPacket
) -> None:
    """After settling, the durable marker replaces the short-lived claim."""
    await processor.process(packet.model_dump_json().encode("utf-8"))
    assert await dedupe.state(packet.idempotency_key) == SETTLED_MARKER


async def test_claim_is_released_when_a_packet_is_permanently_rejected(
    processor: SettlementProcessor, dedupe: DedupeStore, keys, other_keys
) -> None:
    """A packet that can never settle must not hold its key hostage.

    Built so it passes signature verification but fails decryption: signed by
    the right key, but sealed to a different recipient.
    """
    packet = build_packet(keys)
    wrong_recipient = PaymentPacket.create(
        instruction=packet.open_instruction(keys.rsa_private),
        sender_id=packet.sender_id,
        signing_key=keys.signing_private,
        recipient_public_key=other_keys.rsa_public,
    )

    outcome = await processor.process(wrong_recipient.model_dump_json().encode("utf-8"))

    assert not outcome.settled
    assert outcome.code is ErrorCode.DECRYPTION_FAILED
    assert await dedupe.state(wrong_recipient.idempotency_key) is None


# --- Postgres is the durable backstop ---------------------------------------


async def test_postgres_unique_constraint_catches_a_duplicate_when_redis_forgets(
    processor: SettlementProcessor, session_factory, redis_client, packet: PaymentPacket
) -> None:
    """Exactly-once must survive Redis losing its data.

    Settle a packet, wipe Redis so the dedupe layer has amnesia, then replay.
    The Redis claim succeeds this time, so the packet reaches Postgres, and the
    unique constraint is what prevents the second settlement.
    """
    body = packet.model_dump_json().encode("utf-8")
    assert (await processor.process(body)).settled

    await redis_client.flushdb()

    replay = await processor.process(body)
    assert not replay.settled
    assert replay.code is ErrorCode.DUPLICATE_PACKET

    async with session_factory() as session:
        assert await count_settlements(session) == 1


async def test_concurrent_settlement_after_redis_wipe_still_settles_once(
    processor: SettlementProcessor, session_factory, redis_client, keys
) -> None:
    """Worst case: no dedupe cache at all, pure database-level protection."""
    packet = build_packet(keys)
    body = packet.model_dump_json().encode("utf-8")

    async def settle_with_amnesia() -> object:
        await redis_client.flushdb()
        return await processor.process(body)

    outcomes = await asyncio.gather(*(settle_with_amnesia() for _ in range(20)))

    assert sum(1 for outcome in outcomes if outcome.settled) == 1
    async with session_factory() as session:
        assert await count_settlements(session) == 1


# --- What actually got written ----------------------------------------------


async def test_settled_row_matches_the_encrypted_instruction(
    processor: SettlementProcessor, session_factory, keys
) -> None:
    """The settled amount must be exactly what the sender sealed."""
    packet = build_packet(keys, amount_minor=98_765, payee_id="device-carol")
    instruction = packet.open_instruction(keys.rsa_private)

    outcome = await processor.process(packet.model_dump_json().encode("utf-8"))
    assert outcome.settled

    from services.settlement.db import get_settlement

    async with session_factory() as session:
        row = await get_settlement(session, packet.idempotency_key)

    assert row is not None
    assert row.amount_minor == 98_765
    assert row.payee_id == "device-carol"
    assert row.payer_id == instruction.payer_id
    assert row.currency == "INR"
    assert row.packet_id == packet.packet_id
    assert row.sender_id == packet.sender_id


async def test_settlement_reports_settled_status(
    processor: SettlementProcessor, packet: PaymentPacket
) -> None:
    outcome = await processor.process(packet.model_dump_json().encode("utf-8"))
    assert outcome.status is PacketStatus.SETTLED
    assert outcome.code is None
    assert outcome.settlement_id is not None
    assert outcome.idempotency_key == packet.idempotency_key
    assert outcome.retryable is False


async def test_hop_count_is_recorded(processor: SettlementProcessor, session_factory, keys) -> None:
    packet = build_packet(keys)
    for index in range(4):
        packet = packet.append_hop(
            HopRecord(node_id=f"relay-{index}", received_at=datetime.now(UTC))
        )

    await processor.process(packet.model_dump_json().encode("utf-8"))

    from services.settlement.db import get_settlement

    async with session_factory() as session:
        row = await get_settlement(session, packet.idempotency_key)
    assert row is not None
    assert row.hop_count == 4
