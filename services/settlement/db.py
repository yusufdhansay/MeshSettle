"""Database layer for settlement.

One table, `settlements`, and one hard rule: `idempotency_key` is UNIQUE.
Redis is the fast path that rejects duplicates cheaply; this constraint is
the durable backstop that still holds if Redis is flushed, evicts a key, or
loses its dataset. Redis makes duplicate rejection cheap, Postgres makes it
true.

All access goes through SQLAlchemy with bound parameters. There is no
string-interpolated SQL anywhere in this file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from shared.config import settings


class Base(DeclarativeBase):
    """Declarative base for MeshSettle's ORM models."""


class Settlement(Base):
    """A settled payment. One row per successfully settled packet.

    The row is the proof of settlement: if it exists, the payment happened
    exactly once, and no second row for the same packet can ever be written.
    """

    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: The dedupe key derived from the packet's signed region. The uniqueness
    #: guarantee of the whole system lives on this column.
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)

    packet_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    sender_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payee_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Integer minor units (paise). Never a float: this is money.
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    #: When the sender created the packet, versus when we settled it. The gap
    #: between these two is the offline window, which is the whole point.
    packet_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    #: Hops recorded in transit. Untrusted metadata, stored for observability.
    hop_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        # The durable exactly-once guarantee.
        UniqueConstraint("idempotency_key", name="uq_settlements_idempotency_key"),
        # A packet id should also only settle once. Kept separate from the
        # idempotency key so a mismatch between the two is a loud failure
        # rather than a silent second settlement.
        UniqueConstraint("packet_id", name="uq_settlements_packet_id"),
        CheckConstraint("amount_minor > 0", name="ck_settlements_amount_positive"),
        CheckConstraint("hop_count >= 0", name="ck_settlements_hop_count_non_negative"),
        Index("ix_settlements_settled_at", "settled_at"),
        Index("ix_settlements_payer_id", "payer_id"),
    )


class RejectedPacket(Base):
    """A packet that was refused, and why.

    RULES.md requires every rejected packet to be logged with a reason. Logs
    are the primary record, but persisting rejections means the tamper test
    can assert on durable evidence rather than scraping stdout, and the demo
    UI can show rejections alongside settlements.
    """

    __tablename__ = "rejected_packets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: Nullable: a payload too malformed to parse has no derivable key.
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    packet_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    #: One of shared.models.ErrorCode.
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str] = mapped_column(String(256), nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_rejected_packets_reason", "reason"),
        Index("ix_rejected_packets_rejected_at", "rejected_at"),
    )


# --- Engine and session management -------------------------------------------


def create_engine(dsn: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """Build an async engine.

    ``pool_pre_ping`` matters here: the settlement consumer is long-lived and
    may sit idle between bursts of mesh traffic, long enough for Postgres or
    an intermediary to drop a pooled connection.
    """
    return create_async_engine(
        dsn or settings.postgres_dsn,
        echo=echo,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory.

    ``expire_on_commit=False`` so a settlement object stays readable after its
    transaction commits, which the consumer needs in order to log and report
    what it just wrote.
    """
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def create_all(engine: AsyncEngine) -> None:
    """Create tables directly, bypassing Alembic.

    For tests and local throwaway databases only. Real deployments run the
    Alembic migrations so schema changes are versioned and reviewable.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def drop_all(engine: AsyncEngine) -> None:
    """Drop all tables. Tests only."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


# --- Queries used by the consumer and the API -------------------------------


async def settlement_exists(session: AsyncSession, idempotency_key: str) -> bool:
    """Whether a settlement row already exists for this key.

    Used by the crash-recovery path: a Redis claim with no matching row means
    a previous attempt died between claiming and committing.
    """
    statement = select(Settlement.id).where(Settlement.idempotency_key == idempotency_key).limit(1)
    result = await session.execute(statement)
    return result.scalar_one_or_none() is not None


async def get_settlement(session: AsyncSession, idempotency_key: str) -> Settlement | None:
    """Fetch a settlement by its dedupe key."""
    statement = select(Settlement).where(Settlement.idempotency_key == idempotency_key).limit(1)
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def count_settlements(session: AsyncSession, idempotency_key: str | None = None) -> int:
    """Count settlements, optionally for one dedupe key.

    The concurrency test asserts on this: fire N duplicates, expect exactly 1.
    """
    statement = select(func.count()).select_from(Settlement)
    if idempotency_key is not None:
        statement = statement.where(Settlement.idempotency_key == idempotency_key)
    result = await session.execute(statement)
    return int(result.scalar_one())


async def count_rejections(session: AsyncSession, reason: str | None = None) -> int:
    """Count rejected packets, optionally by reason."""
    statement = select(func.count()).select_from(RejectedPacket)
    if reason is not None:
        statement = statement.where(RejectedPacket.reason == reason)
    result = await session.execute(statement)
    return int(result.scalar_one())


async def record_rejection(
    session: AsyncSession,
    *,
    reason: str,
    detail: str,
    idempotency_key: str | None = None,
    packet_id: UUID | None = None,
) -> None:
    """Persist a rejection. Caller controls the transaction boundary."""
    session.add(
        RejectedPacket(
            idempotency_key=idempotency_key,
            packet_id=packet_id,
            reason=reason,
            detail=detail[:256],
            rejected_at=datetime.now(UTC),
        )
    )
