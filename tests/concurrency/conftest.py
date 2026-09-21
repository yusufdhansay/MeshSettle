"""Fixtures for the settlement correctness tests.

These tests run against real Redis and real Postgres, not fakes. That is the
point: the exactly-once claim is a claim about how Redis ``SET NX`` and a
Postgres unique constraint behave under concurrency, and a mock would simply
reproduce whatever assumptions were baked into it.

They skip cleanly when the infrastructure is not reachable, so a plain
`pytest` run does not require Docker. Bring dependencies up with:

    docker run --rm --name meshsettle-pg-test -p 5433:5432 \
        -e POSTGRES_USER=meshsettle -e POSTGRES_PASSWORD=testonly-localdev \
        -e POSTGRES_DB=meshsettle postgres:16-alpine
    docker run --rm --name meshsettle-redis-test -p 6380:6379 redis:7-alpine

Override the endpoints with ``MESHSETTLE_TEST_PG_DSN`` and
``MESHSETTLE_TEST_REDIS_URL``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from services.settlement.db import create_all, create_engine, create_session_factory, drop_all
from services.settlement.dedupe import DedupeStore
from services.settlement.processor import SettlementProcessor
from shared.models import PaymentInstruction, PaymentPacket

TEST_PG_DSN = os.environ.get(
    "MESHSETTLE_TEST_PG_DSN",
    "postgresql+asyncpg://meshsettle:testonly-localdev@localhost:5433/meshsettle",
)
TEST_REDIS_URL = os.environ.get("MESHSETTLE_TEST_REDIS_URL", "redis://localhost:6380/0")


async def _postgres_reachable() -> bool:
    engine = create_engine(TEST_PG_DSN)
    try:
        async with engine.connect():
            return True
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return False
    finally:
        await engine.dispose()


async def _redis_reachable() -> bool:
    client = aioredis.from_url(TEST_REDIS_URL)
    try:
        await client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        await client.aclose()


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A Postgres engine with a freshly created schema per test."""
    if not await _postgres_reachable():
        pytest.skip(f"no Postgres reachable at {TEST_PG_DSN}")

    engine = create_engine(TEST_PG_DSN)
    await drop_all(engine)
    await create_all(engine)
    try:
        yield engine
    finally:
        await drop_all(engine)
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
async def redis_client() -> AsyncIterator[aioredis.Redis]:
    """A Redis client scoped to its own database, flushed around each test."""
    if not await _redis_reachable():
        pytest.skip(f"no Redis reachable at {TEST_REDIS_URL}")

    client = aioredis.from_url(TEST_REDIS_URL, decode_responses=False)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def dedupe(redis_client: aioredis.Redis) -> DedupeStore:
    return DedupeStore(redis_client)


@pytest.fixture
def processor(
    session_factory: async_sessionmaker[AsyncSession],
    dedupe: DedupeStore,
    keys,
) -> SettlementProcessor:
    """A processor wired to real Redis and real Postgres."""
    return SettlementProcessor(
        session_factory=session_factory,
        dedupe=dedupe,
        sender_public_key=keys.signing_public,
        settlement_private_key=keys.rsa_private,
    )


# --- Packet builders ---------------------------------------------------------


def build_packet(
    keys,
    *,
    amount_minor: int = 125_00,
    payer_id: str = "device-alice",
    payee_id: str = "device-bob",
    sender_id: str = "device-alice",
) -> PaymentPacket:
    """A valid, signed, encrypted packet."""
    instruction = PaymentInstruction(
        packet_id=uuid4(),
        payer_id=payer_id,
        payee_id=payee_id,
        amount_minor=amount_minor,
        currency="INR",
        created_at=datetime.now(UTC),
    )
    return PaymentPacket.create(
        instruction=instruction,
        sender_id=sender_id,
        signing_key=keys.signing_private,
        recipient_public_key=keys.rsa_public,
    )


@pytest.fixture
def packet(keys) -> PaymentPacket:
    return build_packet(keys)
