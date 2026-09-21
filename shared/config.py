"""Centralized configuration for all MeshSettle services.

Every value comes from the environment. Nothing is hardcoded, and no
default is ever a real credential (see RULES.md). Import `settings` from
here rather than reading `os.environ` directly, so that validation happens
in exactly one place.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, populated from environment variables or `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Postgres ---
    postgres_user: str = "meshsettle"
    postgres_password: str = ""
    postgres_db: str = "meshsettle"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    #: How long a "this packet is already settled" marker is remembered. Sets
    #: the window inside which a redelivered duplicate is rejected by Redis
    #: without touching Postgres.
    dedupe_ttl_seconds: int = 86_400
    #: How long an in-flight claim is held before it is considered abandoned.
    #: Deliberately short: if a consumer dies between claiming and committing,
    #: the broker redelivers the message and the retry must be able to win the
    #: claim. Too long and a crash stalls that packet; too short and a slow
    #: settlement could be claimed twice (the Postgres unique constraint still
    #: prevents a double write in that case).
    dedupe_claim_ttl_seconds: int = Field(default=60, ge=1)

    # --- RabbitMQ ---
    rabbitmq_user: str = "meshsettle"
    rabbitmq_password: str = ""
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    settlement_queue: str = "meshsettle.settlements"

    # --- Crypto keys (PEM text; "\n" escapes are normalized on load) ---
    sender_signing_private_key_pem: str = ""
    sender_signing_public_key_pem: str = ""
    settlement_rsa_private_key_pem: str = ""
    settlement_rsa_public_key_pem: str = ""

    # --- Mesh simulation ---
    #: How many device-to-device hops a packet makes before a node with
    #: connectivity bridges it to the queue.
    mesh_hop_count: int = Field(default=2, ge=0)
    #: Hard ceiling on recorded hops. Loop protection: a misconfigured or
    #: malicious relay ring must not be able to circulate a packet forever.
    mesh_hop_limit: int = Field(default=16, ge=1)
    #: Identity of this relay node, recorded in each hop record.
    mesh_node_id: str = "relay-1"
    mesh_relay_url: str = "http://localhost:8002"
    bridge_url: str = "http://localhost:8003"
    #: Timeout for a single forwarding call between mesh nodes, in seconds.
    mesh_forward_timeout_seconds: float = Field(default=5.0, gt=0)

    # --- Rate limiting ---
    rate_limit_per_minute: int = Field(default=60, ge=1)

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def postgres_dsn(self) -> str:
        """Async SQLAlchemy DSN. Credentials come from env, never inlined."""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def postgres_sync_dsn(self) -> str:
        """Sync DSN, used by Alembic migrations only."""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rabbitmq_url(self) -> str:
        return (
            f"amqp://{self.rabbitmq_user}:{self.rabbitmq_password}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/"
        )


def normalize_pem(raw: str) -> str:
    """Turn an env-injected PEM back into real PEM text.

    Environment variables cannot hold literal newlines portably, so PEMs are
    stored with ``\\n`` escapes. This converts them back. Returns an empty
    string unchanged so callers can detect "not configured".
    """
    if not raw:
        return ""
    return raw.replace("\\n", "\n").strip() + "\n"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor, so `.env` is parsed once per process."""
    return Settings()


settings = get_settings()
