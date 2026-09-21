"""Settlement service: queue consumer plus a read-only API.

The consumer is the real work; the HTTP surface exists so the demo UI can
show what settled, so operators can see counters, and so Kubernetes has
something to probe. Every route here is read-only. Settlement happens only
in response to a queue message, never in response to an HTTP request, which
is what keeps the queue boundary meaningful.

Note: no ``from __future__ import annotations`` here; the rate-limit
decorator wraps route functions and FastAPI would resolve string annotations
against the wrapper's module globals.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.settlement.consumer import SettlementConsumer
from services.settlement.db import (
    Settlement,
    create_engine,
    create_session_factory,
    get_settlement,
)
from services.settlement.dedupe import DedupeStore, create_redis_client
from services.settlement.processor import SettlementMetrics, SettlementProcessor
from shared.config import settings
from shared.crypto import sender_signing_public_key, settlement_rsa_private_key
from shared.logging import configure_logging, get_logger
from shared.models import ErrorCode, ErrorResponse

SERVICE_NAME = "settlement"

logger = get_logger(SERVICE_NAME)


# --- Response models ---------------------------------------------------------


class SettlementView(BaseModel):
    """A settled payment, as shown in the demo UI."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    idempotency_key: str
    packet_id: UUID
    sender_id: str
    payer_id: str
    payee_id: str
    amount_minor: int
    currency: str
    packet_created_at: datetime
    settled_at: datetime
    hop_count: int


class MetricsView(BaseModel):
    """Counters and queue depth. Every number here is observed, not estimated."""

    model_config = ConfigDict(extra="forbid")

    settled: int
    rejected: int
    duplicates: int
    invalid_signature: int
    malformed: int
    decryption_failed: int
    internal_errors: int
    queue_depth: int | None = None


class HealthView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    service: str
    redis: bool
    database: bool
    consuming: bool


# --- Dependencies ------------------------------------------------------------


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    raise RuntimeError("session factory dependency was not configured")


def get_dedupe_store() -> DedupeStore:
    raise RuntimeError("dedupe store dependency was not configured")


def get_metrics() -> SettlementMetrics:
    raise RuntimeError("metrics dependency was not configured")


def get_consumer() -> SettlementConsumer | None:
    """The running consumer, or ``None`` when the API runs without one."""
    return None


SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Dedupe = Annotated[DedupeStore, Depends(get_dedupe_store)]
Metrics = Annotated[SettlementMetrics, Depends(get_metrics)]
Consumer = Annotated[SettlementConsumer | None, Depends(get_consumer)]


# --- Application -------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire up Redis, Postgres, and the queue consumer for a real deployment."""
    configure_logging(service=SERVICE_NAME)

    engine = create_engine()
    session_factory = create_session_factory(engine)
    redis_client = create_redis_client()
    dedupe = DedupeStore(redis_client)
    metrics = SettlementMetrics()

    processor = SettlementProcessor(
        session_factory=session_factory,
        dedupe=dedupe,
        sender_public_key=sender_signing_public_key(),
        settlement_private_key=settlement_rsa_private_key(),
        metrics=metrics,
    )
    consumer = SettlementConsumer(processor)
    await consumer.start()

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis_client = redis_client
    app.state.dedupe = dedupe
    app.state.metrics = metrics
    app.state.processor = processor
    app.state.consumer = consumer

    logger.info("settlement.startup", queue=consumer.queue_name)
    try:
        yield
    finally:
        await consumer.stop()
        await redis_client.aclose()
        await engine.dispose()
        logger.info("settlement.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="MeshSettle Settlement",
        description=(
            "Consumes relayed packets and settles them exactly once. "
            "Read-only HTTP surface. Simulation only; moves no real money."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter

    # Resolve dependencies from app state, which lifespan populates. Tests
    # override these directly and never start a consumer.
    app.dependency_overrides[get_session_factory] = lambda: app.state.session_factory
    app.dependency_overrides[get_dedupe_store] = lambda: app.state.dedupe
    app.dependency_overrides[get_metrics] = lambda: app.state.metrics
    app.dependency_overrides[get_consumer] = lambda: getattr(app.state, "consumer", None)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(_request: Request, exc: RateLimitExceeded) -> JSONResponse:
        logger.warning("settlement.rate_limited", limit=str(exc.detail))
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=ErrorResponse(
                code=ErrorCode.INTERNAL_ERROR, detail="rate limit exceeded"
            ).model_dump(mode="json"),
        )

    @app.get("/healthz", response_model=HealthView, tags=["ops"])
    async def healthz(
        session_factory: SessionFactory,
        dedupe: Dedupe,
        consumer: Consumer,
    ) -> HealthView:
        """Report dependency health. Used by Kubernetes probes."""
        redis_ok = await dedupe.ping()

        database_ok = True
        try:
            async with session_factory() as session:
                await session.execute(select(1))
        except Exception:  # noqa: BLE001 - a probe reports, it does not raise
            database_ok = False

        consuming = consumer is not None
        healthy = redis_ok and database_ok
        return HealthView(
            status="ok" if healthy else "degraded",
            service=SERVICE_NAME,
            redis=redis_ok,
            database=database_ok,
            consuming=consuming,
        )

    @app.get("/metrics", response_model=MetricsView, tags=["ops"])
    async def metrics_endpoint(metrics: Metrics, consumer: Consumer) -> MetricsView:
        """Observed counters plus current queue depth."""
        depth = await consumer.queue_depth() if consumer is not None else None
        return MetricsView(**metrics.snapshot(), queue_depth=depth)

    @app.get(
        "/settlements/{idempotency_key}",
        response_model=SettlementView,
        tags=["settlements"],
        responses={404: {"model": ErrorResponse}},
    )
    @limiter.limit(f"{settings.rate_limit_per_minute}/minute")
    async def read_settlement(
        request: Request,  # noqa: ARG001 - required by the rate limiter
        idempotency_key: str,
        session_factory: SessionFactory,
    ):
        """Look up one settlement by its dedupe key."""
        async with session_factory() as session:
            settlement = await get_settlement(session, idempotency_key)

        if settlement is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=ErrorResponse(
                    code=ErrorCode.MALFORMED_PAYLOAD,
                    detail="no settlement for that idempotency key",
                ).model_dump(mode="json"),
            )
        return SettlementView.model_validate(settlement)

    @app.get("/settlements", response_model=list[SettlementView], tags=["settlements"])
    @limiter.limit(f"{settings.rate_limit_per_minute}/minute")
    async def list_settlements(
        request: Request,  # noqa: ARG001 - required by the rate limiter
        session_factory: SessionFactory,
        limit: int = 20,
    ) -> list[SettlementView]:
        """Most recent settlements, for the demo UI."""
        capped = max(1, min(limit, 100))
        async with session_factory() as session:
            result = await session.execute(
                select(Settlement).order_by(desc(Settlement.settled_at)).limit(capped)
            )
            rows = result.scalars().all()
        return [SettlementView.model_validate(row) for row in rows]

    return app


app = create_app()
