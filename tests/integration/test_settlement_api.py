"""Tests for the settlement service's read-only HTTP surface.

Two things worth pinning down beyond the obvious CRUD behaviour:

* the API cannot cause a settlement. Settlement happens only via the queue,
  and that boundary is what makes the architecture more than a function call.
* health reporting tells the truth about dependencies, because a probe that
  reports healthy while Redis is down is worse than no probe.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import redis.asyncio as aioredis

from services.settlement.app import (
    create_app,
    get_consumer,
    get_dedupe_store,
    get_metrics,
    get_session_factory,
)
from services.settlement.db import (
    create_all,
    create_engine,
    create_session_factory,
    drop_all,
)
from services.settlement.dedupe import DedupeStore
from services.settlement.processor import SettlementMetrics, SettlementProcessor
from shared.models import PaymentInstruction, PaymentPacket

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get(
    "MESHSETTLE_TEST_PG_DSN",
    "postgresql+asyncpg://meshsettle:testonly-localdev@localhost:5433/meshsettle",
)
REDIS_URL = os.environ.get("MESHSETTLE_TEST_REDIS_URL", "redis://localhost:6380/0")


@pytest.fixture
async def api(keys) -> AsyncIterator[dict]:
    """A settlement API wired to real Postgres and Redis, with no consumer."""
    engine = create_engine(PG_DSN)
    try:
        async with engine.connect():
            pass
    except Exception:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"no Postgres reachable at {PG_DSN}")

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=False)
    try:
        await redis_client.ping()
    except Exception:  # noqa: BLE001
        await redis_client.aclose()
        await engine.dispose()
        pytest.skip(f"no Redis reachable at {REDIS_URL}")

    await drop_all(engine)
    await create_all(engine)
    session_factory = create_session_factory(engine)
    await redis_client.flushdb()

    dedupe = DedupeStore(redis_client)
    metrics = SettlementMetrics()
    processor = SettlementProcessor(
        session_factory=session_factory,
        dedupe=dedupe,
        sender_public_key=keys.signing_public,
        settlement_private_key=keys.rsa_private,
        metrics=metrics,
    )

    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_dedupe_store] = lambda: dedupe
    app.dependency_overrides[get_metrics] = lambda: metrics
    app.dependency_overrides[get_consumer] = lambda: None

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://settlement")
    try:
        yield {"client": client, "processor": processor, "app": app}
    finally:
        await client.aclose()
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


# --- Health ------------------------------------------------------------------


async def test_healthz_reports_dependency_state(api) -> None:
    response = await api["client"].get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "settlement"
    assert body["redis"] is True
    assert body["database"] is True
    # No consumer is attached in this fixture.
    assert body["consuming"] is False


async def test_healthz_reports_degraded_when_redis_is_unreachable(api) -> None:
    """A probe must not claim health it cannot verify."""
    broken = aioredis.from_url("redis://localhost:6399/0", decode_responses=False)
    api["app"].dependency_overrides[get_dedupe_store] = lambda: DedupeStore(broken)
    try:
        response = await api["client"].get("/healthz")
    finally:
        await broken.aclose()

    body = response.json()
    assert body["redis"] is False
    assert body["status"] == "degraded"


# --- Metrics -----------------------------------------------------------------


async def test_metrics_start_at_zero(api) -> None:
    body = (await api["client"].get("/metrics")).json()
    assert body["settled"] == 0
    assert body["rejected"] == 0
    assert body["queue_depth"] is None


async def test_metrics_reflect_real_outcomes(api, keys) -> None:
    packet = _packet(keys)
    body_bytes = packet.model_dump_json().encode("utf-8")

    await api["processor"].process(body_bytes)
    await api["processor"].process(body_bytes)  # duplicate
    await api["processor"].process(b"not json")  # malformed

    body = (await api["client"].get("/metrics")).json()
    assert body["settled"] == 1
    assert body["duplicates"] == 1
    assert body["malformed"] == 1
    assert body["rejected"] == 2


# --- Settlement lookup -------------------------------------------------------


async def test_settlement_can_be_read_back_after_settling(api, keys) -> None:
    packet = _packet(keys, amount_minor=777_00)
    await api["processor"].process(packet.model_dump_json().encode("utf-8"))

    response = await api["client"].get(f"/settlements/{packet.idempotency_key}")

    assert response.status_code == 200
    body = response.json()
    assert body["idempotency_key"] == packet.idempotency_key
    assert body["amount_minor"] == 777_00
    assert body["packet_id"] == str(packet.packet_id)
    assert body["hop_count"] == 0


async def test_unknown_settlement_returns_404_with_an_error_code(api) -> None:
    response = await api["client"].get("/settlements/settle:" + "f" * 64)

    assert response.status_code == 404
    assert set(response.json()) == {"code", "detail"}


async def test_settlements_list_is_newest_first(api, keys) -> None:
    packets = [_packet(keys, amount_minor=100 + index) for index in range(5)]
    for packet in packets:
        await api["processor"].process(packet.model_dump_json().encode("utf-8"))

    body = (await api["client"].get("/settlements")).json()

    assert len(body) == 5
    settled_times = [row["settled_at"] for row in body]
    assert settled_times == sorted(settled_times, reverse=True)


async def test_settlements_list_limit_is_capped(api, keys) -> None:
    """An unbounded list endpoint is a denial-of-service waiting to happen."""
    for index in range(5):
        packet = _packet(keys, amount_minor=200 + index)
        await api["processor"].process(packet.model_dump_json().encode("utf-8"))

    assert len((await api["client"].get("/settlements?limit=2")).json()) == 2
    # Absurd limits are clamped rather than honoured or rejected.
    assert len((await api["client"].get("/settlements?limit=100000")).json()) == 5
    assert len((await api["client"].get("/settlements?limit=0")).json()) == 1


async def test_api_cannot_cause_a_settlement(api, keys) -> None:
    """There is no write path over HTTP. Settlement is queue-only by design."""
    packet = _packet(keys)

    for method, path in [
        ("POST", "/settlements"),
        ("PUT", f"/settlements/{packet.idempotency_key}"),
        ("DELETE", f"/settlements/{packet.idempotency_key}"),
        ("POST", "/settle"),
    ]:
        response = await api["client"].request(method, path, content=packet.model_dump_json())
        assert response.status_code in {404, 405}, f"{method} {path} should not exist"

    body = (await api["client"].get("/metrics")).json()
    assert body["settled"] == 0
