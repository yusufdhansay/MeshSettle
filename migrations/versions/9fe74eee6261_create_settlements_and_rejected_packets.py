"""create settlements and rejected_packets

Revision ID: 9fe74eee6261
Revises:
Create Date: 2026-09-21 17:32:26.529666
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9fe74eee6261"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `uq_settlements_idempotency_key` is the durable exactly-once guarantee:
    # Redis rejects duplicates cheaply, but this constraint is what makes the
    # guarantee survive a cache flush or an expired claim.
    op.create_table(
        "rejected_packets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=True),
        sa.Column("packet_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.String(length=256), nullable=False),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rejected_packets_reason", "rejected_packets", ["reason"], unique=False)
    op.create_index(
        "ix_rejected_packets_rejected_at", "rejected_packets", ["rejected_at"], unique=False
    )
    op.create_table(
        "settlements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("packet_id", sa.UUID(), nullable=False),
        sa.Column("sender_id", sa.String(length=64), nullable=False),
        sa.Column("payer_id", sa.String(length=64), nullable=False),
        sa.Column("payee_id", sa.String(length=64), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("packet_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "settled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("hop_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("amount_minor > 0", name="ck_settlements_amount_positive"),
        sa.CheckConstraint("hop_count >= 0", name="ck_settlements_hop_count_non_negative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_settlements_idempotency_key"),
        sa.UniqueConstraint("packet_id", name="uq_settlements_packet_id"),
    )
    op.create_index("ix_settlements_payer_id", "settlements", ["payer_id"], unique=False)
    op.create_index("ix_settlements_settled_at", "settlements", ["settled_at"], unique=False)


def downgrade() -> None:

    op.drop_index("ix_settlements_settled_at", table_name="settlements")
    op.drop_index("ix_settlements_payer_id", table_name="settlements")
    op.drop_table("settlements")
    op.drop_index("ix_rejected_packets_rejected_at", table_name="rejected_packets")
    op.drop_index("ix_rejected_packets_reason", table_name="rejected_packets")
    op.drop_table("rejected_packets")
