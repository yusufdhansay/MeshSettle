"""Redis deduplication: the exactly-once gate.

This is the smallest and most important module in the project. Everything
about the correctness claim comes down to one Redis command:

    SET <idempotency_key> claimed NX EX <claim_ttl>

``NX`` makes it atomic. When N consumers race on the same packet, exactly one
``SET`` returns true. There is no read-then-write window, no lock to acquire
and release, and no retry loop, so there is no interleaving in which two
consumers both believe they are first. A read-then-write
(``GET`` then ``SET``) would be wrong here: two consumers could both read
"absent" before either wrote.

Two marker states, with deliberately different lifetimes:

``claimed`` (short TTL)
    A consumer is working on this packet right now. Short-lived on purpose:
    if that consumer dies between claiming and committing, the claim must
    expire so the broker's redelivery can win it and finish the job.
    Otherwise a crash would strand that payment until the long TTL elapsed.

``settled`` (long TTL)
    The packet is done. This is what makes the common duplicate case cheap:
    a redelivered copy is rejected here and never reaches Postgres at all.

Why a duplicate that sees ``claimed`` is dropped rather than retried: it is a
redundant copy of an instruction another consumer is already settling. If
that consumer fails, its own message is redelivered (the consumer acks only
after the database commits), so nothing is lost by dropping the extra copy.
"""

from __future__ import annotations

from enum import StrEnum, auto

import redis.asyncio as aioredis

from shared.config import settings
from shared.logging import get_logger

logger = get_logger("settlement.dedupe")

CLAIMED_MARKER = "claimed"
SETTLED_MARKER = "settled"


class ClaimOutcome(StrEnum):
    """Result of trying to claim a packet for settlement."""

    #: This consumer won the claim and must proceed with settlement.
    CLAIMED = auto()
    #: Another consumer is settling this packet right now. Drop this copy.
    IN_FLIGHT = auto()
    #: This packet is already settled. Reject without touching Postgres.
    ALREADY_SETTLED = auto()


class DedupeStore:
    """Atomic claim/settle bookkeeping backed by Redis."""

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        claim_ttl_seconds: int | None = None,
        settled_ttl_seconds: int | None = None,
    ) -> None:
        self._client = client
        self._claim_ttl = claim_ttl_seconds or settings.dedupe_claim_ttl_seconds
        self._settled_ttl = settled_ttl_seconds or settings.dedupe_ttl_seconds

    async def claim(self, idempotency_key: str) -> ClaimOutcome:
        """Attempt to claim a packet for settlement.

        Returns:
            :attr:`ClaimOutcome.CLAIMED` if this caller may proceed, and one of
            the duplicate outcomes otherwise.
        """
        won = await self._client.set(
            idempotency_key,
            CLAIMED_MARKER,
            nx=True,
            ex=self._claim_ttl,
        )
        if won:
            return ClaimOutcome.CLAIMED

        # Lost the race. Distinguish "already finished" from "in progress" so
        # the two cases can be logged and counted separately.
        current = await self._client.get(idempotency_key)
        marker = _as_text(current)
        if marker == SETTLED_MARKER:
            return ClaimOutcome.ALREADY_SETTLED
        return ClaimOutcome.IN_FLIGHT

    async def mark_settled(self, idempotency_key: str) -> None:
        """Promote a claim to the durable settled marker.

        Called only after the Postgres transaction has committed. Overwrites
        the short-lived claim with the long-lived marker, which is what makes
        later duplicates cheap to reject.
        """
        await self._client.set(idempotency_key, SETTLED_MARKER, ex=self._settled_ttl)

    async def release_claim(self, idempotency_key: str) -> None:
        """Drop a claim without settling.

        Used when settlement fails for a reason that makes the packet worth
        retrying. Releasing immediately is better than waiting out the TTL,
        because it lets the broker's redelivery proceed straight away.
        """
        await self._client.delete(idempotency_key)

    async def state(self, idempotency_key: str) -> str | None:
        """Current marker for a key, or ``None`` if absent. For tests and ops."""
        return _as_text(await self._client.get(idempotency_key))

    async def ping(self) -> bool:
        """Whether Redis is reachable. Used by the health endpoint."""
        try:
            return bool(await self._client.ping())
        except Exception:  # noqa: BLE001 - health checks report, they do not raise
            return False


def _as_text(value: bytes | str | None) -> str | None:
    """Normalize a Redis value to text regardless of decode settings."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def create_redis_client(url: str | None = None) -> aioredis.Redis:
    """Build an async Redis client.

    ``decode_responses=False`` keeps values as bytes so behaviour does not
    depend on client-side decoding; :func:`_as_text` normalizes at the edge.
    """
    return aioredis.from_url(
        url or settings.redis_url,
        decode_responses=False,
        health_check_interval=30,
    )
