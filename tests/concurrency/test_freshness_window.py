"""Freshness window: a stale packet must not settle, however valid it looks.

Why this exists. Exactly-once stops a packet settling *twice*. It says nothing
about a packet that has never settled *once*. Without an expiry, a packet
captured off the mesh and held back is spendable forever: its signature stays
valid, its AAD stays consistent, and no settlement row exists yet, so the
Postgres unique constraint has nothing to catch. These tests pin down the bound
on how long such a packet stays useful.

Run against real Redis and Postgres, like the rest of this directory, because
the point of several of them is the *interaction* with those two: specifically
that a stale packet is refused before it ever touches the dedupe layer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.settlement.db import count_rejections, count_settlements
from services.settlement.processor import SettlementProcessor
from shared.models import ErrorCode, PacketStatus, PaymentInstruction, PaymentPacket

pytestmark = pytest.mark.integration

#: Short window used by most tests here, so the boundary can be driven exactly
#: rather than by waiting. The production default is 24 hours.
WINDOW_SECONDS = 3600
SKEW_SECONDS = 300


def _packet_aged(keys, *, age: timedelta, amount_minor: int = 125_00) -> PaymentPacket:
    """Build a properly signed packet whose header is dated in the past.

    Signed at its stated `created_at`, so the signature is genuinely valid for
    that timestamp. This is the realistic shape of the threat: not a forgery,
    but a real packet that has simply been held back.
    """
    created_at = datetime.now(UTC) - age
    instruction = PaymentInstruction(
        packet_id=uuid4(),
        payer_id="device-alice",
        payee_id="device-bob",
        amount_minor=amount_minor,
        currency="INR",
        created_at=created_at,
    )
    return PaymentPacket.create(
        instruction=instruction,
        sender_id="device-alice",
        signing_key=keys.signing_private,
        recipient_public_key=keys.rsa_public,
        created_at=created_at,
    )


@pytest.fixture
def strict_processor(session_factory, dedupe, keys) -> SettlementProcessor:
    """A processor with a one-hour window and the default skew tolerance."""
    return SettlementProcessor(
        session_factory=session_factory,
        dedupe=dedupe,
        sender_public_key=keys.signing_public,
        settlement_private_key=keys.rsa_private,
        freshness_window_seconds=WINDOW_SECONDS,
        clock_skew_tolerance_seconds=SKEW_SECONDS,
    )


# --- The boundary ------------------------------------------------------------


async def test_packet_just_inside_the_window_settles(
    strict_processor: SettlementProcessor, session_factory, keys
) -> None:
    packet = _packet_aged(keys, age=timedelta(seconds=WINDOW_SECONDS - 5))

    outcome = await strict_processor.process(packet.model_dump_json().encode("utf-8"))

    assert outcome.settled, f"expected a settlement, got {outcome.code}: {outcome.detail}"
    async with session_factory() as session:
        assert await count_settlements(session, packet.idempotency_key) == 1


async def test_packet_just_outside_the_window_is_expired(
    strict_processor: SettlementProcessor, session_factory, keys
) -> None:
    packet = _packet_aged(keys, age=timedelta(seconds=WINDOW_SECONDS + 5))

    outcome = await strict_processor.process(packet.model_dump_json().encode("utf-8"))

    assert not outcome.settled
    assert outcome.code is ErrorCode.PACKET_EXPIRED
    async with session_factory() as session:
        assert await count_settlements(session) == 0


async def test_packet_exactly_at_the_window_edge_is_accepted(
    strict_processor: SettlementProcessor, keys
) -> None:
    """The edge is inclusive, so a "24 hour" window really accepts 24 hours.

    Uses a tiny margin below the boundary because a few milliseconds elapse
    between constructing the packet and processing it; the assertion is that
    the edge is not refused by a hair.
    """
    packet = _packet_aged(keys, age=timedelta(seconds=WINDOW_SECONDS, milliseconds=-50))

    outcome = await strict_processor.process(packet.model_dump_json().encode("utf-8"))
    assert outcome.settled, f"edge should be accepted, got {outcome.code}"


@pytest.mark.parametrize(
    "age",
    [
        timedelta(days=1),
        timedelta(days=7),
        timedelta(days=30),
        timedelta(days=365),
    ],
)
async def test_clearly_stale_packets_are_expired(
    strict_processor: SettlementProcessor, session_factory, keys, age: timedelta
) -> None:
    packet = _packet_aged(keys, age=age)

    outcome = await strict_processor.process(packet.model_dump_json().encode("utf-8"))

    assert not outcome.settled
    assert outcome.code is ErrorCode.PACKET_EXPIRED
    assert outcome.status is PacketStatus.REJECTED
    async with session_factory() as session:
        assert await count_settlements(session) == 0


# --- Ordering: a stale packet must not consume a dedupe claim ----------------


async def test_expired_packet_never_reaches_the_redis_claim(
    strict_processor: SettlementProcessor, dedupe, keys
) -> None:
    """The freshness check must precede the claim.

    Verified the same way the duplicate path is proven not to touch Postgres:
    swap in a dedupe store that raises if anyone tries to claim. If the
    freshness check ran after the claim, this would blow up instead of
    returning a clean rejection.
    """

    class ExplodingDedupe:
        async def claim(self, idempotency_key: str):  # noqa: ANN202
            raise AssertionError("an expired packet must not reach the dedupe claim")

        async def mark_settled(self, idempotency_key: str) -> None:
            raise AssertionError("an expired packet must not be marked settled")

        async def release_claim(self, idempotency_key: str) -> None:
            raise AssertionError("an expired packet holds no claim to release")

    packet = _packet_aged(keys, age=timedelta(days=30))
    strict_processor._dedupe = ExplodingDedupe()  # type: ignore[assignment]

    outcome = await strict_processor.process(packet.model_dump_json().encode("utf-8"))

    assert not outcome.settled
    assert outcome.code is ErrorCode.PACKET_EXPIRED


async def test_expired_packet_leaves_no_trace_in_redis(
    strict_processor: SettlementProcessor, dedupe, keys
) -> None:
    """Its idempotency key must stay free for the genuine packet.

    If a stale packet burned its key, an attacker could pre-emptively expire
    keys belonging to packets that have not been submitted yet.
    """
    packet = _packet_aged(keys, age=timedelta(days=30))

    await strict_processor.process(packet.model_dump_json().encode("utf-8"))

    assert await dedupe.state(packet.idempotency_key) is None


# --- Future-dated packets ----------------------------------------------------


async def test_packet_within_clock_skew_tolerance_settles(
    strict_processor: SettlementProcessor, keys
) -> None:
    """Sender and settlement clocks are not synchronized; small skew is normal."""
    packet = _packet_aged(keys, age=timedelta(seconds=-(SKEW_SECONDS - 30)))

    outcome = await strict_processor.process(packet.model_dump_json().encode("utf-8"))
    assert outcome.settled, f"expected tolerance of small skew, got {outcome.code}"


@pytest.mark.parametrize("ahead", [timedelta(hours=2), timedelta(days=1), timedelta(days=30)])
async def test_future_dated_packet_is_refused(
    strict_processor: SettlementProcessor, session_factory, keys, ahead: timedelta
) -> None:
    """A forged future timestamp is how you postpone expiry, so bound it."""
    packet = _packet_aged(keys, age=-ahead)

    outcome = await strict_processor.process(packet.model_dump_json().encode("utf-8"))

    assert not outcome.settled
    assert outcome.code is ErrorCode.PACKET_NOT_YET_VALID
    async with session_factory() as session:
        assert await count_settlements(session) == 0


async def test_future_and_past_rejections_are_distinguishable(
    strict_processor: SettlementProcessor, session_factory, keys
) -> None:
    """Two codes, because the two causes need different operator responses."""
    await strict_processor.process(
        _packet_aged(keys, age=timedelta(days=30)).model_dump_json().encode("utf-8")
    )
    await strict_processor.process(
        _packet_aged(keys, age=-timedelta(days=30)).model_dump_json().encode("utf-8")
    )

    async with session_factory() as session:
        assert await count_rejections(session, ErrorCode.PACKET_EXPIRED.value) == 1
        assert await count_rejections(session, ErrorCode.PACKET_NOT_YET_VALID.value) == 1

    assert strict_processor.metrics.expired == 1
    assert strict_processor.metrics.not_yet_valid == 1


# --- Durable evidence --------------------------------------------------------


async def test_expiry_rejection_is_persisted_with_its_reason(
    strict_processor: SettlementProcessor, session_factory, keys
) -> None:
    packet = _packet_aged(keys, age=timedelta(days=30))

    outcome = await strict_processor.process(packet.model_dump_json().encode("utf-8"))

    async with session_factory() as session:
        assert await count_rejections(session, ErrorCode.PACKET_EXPIRED.value) == 1
    # The detail should say how old it was, so an operator can tell a slow mesh
    # from a replay attempt.
    assert "old" in outcome.detail
    assert str(WINDOW_SECONDS) in outcome.detail


async def test_metrics_count_expiries_separately(
    strict_processor: SettlementProcessor, keys
) -> None:
    for _ in range(4):
        await strict_processor.process(
            _packet_aged(keys, age=timedelta(days=30)).model_dump_json().encode("utf-8")
        )

    snapshot = strict_processor.metrics.snapshot()
    assert snapshot["expired"] == 4
    assert snapshot["rejected"] == 4
    assert snapshot["settled"] == 0
    # Not lumped in with the pre-existing buckets.
    assert snapshot["duplicates"] == 0
    assert snapshot["invalid_signature"] == 0
    assert snapshot["malformed"] == 0


# --- The default window ------------------------------------------------------


async def test_default_window_accepts_a_recent_packet(session_factory, dedupe, keys) -> None:
    """With the configured default, an ordinary packet still settles."""
    processor = SettlementProcessor(
        session_factory=session_factory,
        dedupe=dedupe,
        sender_public_key=keys.signing_public,
        settlement_private_key=keys.rsa_private,
    )
    packet = _packet_aged(keys, age=timedelta(minutes=5))

    outcome = await processor.process(packet.model_dump_json().encode("utf-8"))
    assert outcome.settled


async def test_default_window_is_twenty_four_hours(session_factory, dedupe, keys) -> None:
    """A packet 23h old settles; one 25h old does not, on the default config."""
    processor = SettlementProcessor(
        session_factory=session_factory,
        dedupe=dedupe,
        sender_public_key=keys.signing_public,
        settlement_private_key=keys.rsa_private,
    )

    fresh = await processor.process(
        _packet_aged(keys, age=timedelta(hours=23)).model_dump_json().encode("utf-8")
    )
    stale = await processor.process(
        _packet_aged(keys, age=timedelta(hours=25)).model_dump_json().encode("utf-8")
    )

    assert fresh.settled
    assert not stale.settled
    assert stale.code is ErrorCode.PACKET_EXPIRED


# --- Interaction with the rest of the pipeline -------------------------------


async def test_a_tampered_stale_packet_is_caught_by_the_signature_first(
    strict_processor: SettlementProcessor, keys
) -> None:
    """Signature verification still precedes freshness.

    Freshness reads `created_at`, which is only trustworthy once the signature
    has been checked. A packet that is both stale and tampered must report the
    signature failure, proving the check order.
    """
    packet = _packet_aged(keys, age=timedelta(days=30))
    payload = json.loads(packet.model_dump_json())
    payload["sender_id"] = "device-attacker"

    outcome = await strict_processor.process(json.dumps(payload).encode("utf-8"))

    assert outcome.code is ErrorCode.INVALID_SIGNATURE


async def test_expiry_does_not_break_the_duplicate_guarantee(
    strict_processor: SettlementProcessor, session_factory, keys
) -> None:
    """A fresh packet settles once and its replays are still duplicates."""
    packet = _packet_aged(keys, age=timedelta(minutes=1))
    body = packet.model_dump_json().encode("utf-8")

    first = await strict_processor.process(body)
    assert first.settled

    for _ in range(5):
        repeat = await strict_processor.process(body)
        assert repeat.code is ErrorCode.DUPLICATE_PACKET

    async with session_factory() as session:
        assert await count_settlements(session) == 1


async def test_a_settled_packet_that_later_goes_stale_is_still_a_duplicate(
    session_factory, dedupe, keys
) -> None:
    """Order of precedence when a packet is both already-settled and expired.

    Settle it inside a generous window, then re-present it to a processor whose
    window has effectively closed. It must still be refused; which code wins is
    a detail, but it must never settle again.
    """
    lenient = SettlementProcessor(
        session_factory=session_factory,
        dedupe=dedupe,
        sender_public_key=keys.signing_public,
        settlement_private_key=keys.rsa_private,
        freshness_window_seconds=86_400,
    )
    packet = _packet_aged(keys, age=timedelta(hours=2))
    body = packet.model_dump_json().encode("utf-8")
    assert (await lenient.process(body)).settled

    strict = SettlementProcessor(
        session_factory=session_factory,
        dedupe=dedupe,
        sender_public_key=keys.signing_public,
        settlement_private_key=keys.rsa_private,
        freshness_window_seconds=60,
    )
    again = await strict.process(body)

    assert not again.settled
    assert (
        again.code is ErrorCode.PACKET_EXPIRED
    ), "freshness precedes the claim, so the stale check is expected to win here"
    async with session_factory() as session:
        assert await count_settlements(session) == 1
