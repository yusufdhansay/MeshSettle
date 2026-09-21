"""Mesh relay: simulates one device-to-device hop.

In the real system this is a phone passing a packet to another phone over
Bluetooth or NFC. Here it is an HTTP call, but the important property is the
same: the relay is a dumb carrier. It cannot read the payment (it has no RSA
private key), it cannot alter the packet without invalidating the signature,
and it makes no settlement decision.

What it does do:

* verifies the signature and drops bad packets early, so garbage does not
  consume further hops (RULES.md requires every packet-accepting endpoint to
  verify before doing anything else with the payload)
* appends an unsigned hop record for observability
* forwards to the next relay, or to the bridge once the configured hop count
  is reached

Note: no ``from __future__ import annotations`` here, because the rate-limit
decorator wraps route functions and FastAPI would resolve string annotations
against the wrapper's module globals.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from shared.config import settings
from shared.crypto import SignatureVerificationError, sender_signing_public_key
from shared.logging import configure_logging, get_logger, safe_fingerprint
from shared.models import (
    ErrorCode,
    ErrorResponse,
    HopRecord,
    PacketStatus,
    PaymentPacket,
    RelayResponse,
)

SERVICE_NAME = "mesh_relay"

logger = get_logger(SERVICE_NAME)


class RelayError(Exception):
    """A packet this node refuses to forward."""

    def __init__(self, code: ErrorCode, detail: str, http_status: int) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.http_status = http_status


# --- Node configuration ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelayConfig:
    """This node's identity and forwarding targets.

    Injected rather than read from globals at request time, because each
    relay is its own deployment with its own ``MESH_NODE_ID``, and because a
    test needs to wire an explicit chain (relay-1 to relay-2 to bridge).
    """

    node_id: str
    next_relay_url: str
    bridge_url: str
    hop_count_target: int
    hop_limit: int

    @classmethod
    def from_settings(cls) -> "RelayConfig":
        return cls(
            node_id=settings.mesh_node_id,
            next_relay_url=settings.mesh_relay_url,
            bridge_url=settings.bridge_url,
            hop_count_target=settings.mesh_hop_count,
            hop_limit=settings.mesh_hop_limit,
        )

    def next_destination(self, hop_count: int) -> tuple[str, PacketStatus]:
        """Decide where a packet goes after this hop.

        Args:
            hop_count: hops already recorded, including this node's.

        Returns:
            The absolute URL to forward to, and the status to report.
        """
        if hop_count >= self.hop_count_target:
            # This node is the one with connectivity.
            return f"{self.bridge_url.rstrip('/')}/bridge", PacketStatus.BRIDGED
        return f"{self.next_relay_url.rstrip('/')}/relay", PacketStatus.RELAYED


# --- Dependencies ------------------------------------------------------------


def get_sender_public_key() -> Ed25519PublicKey:
    """The sender's verification key, so this node can drop forged packets."""
    return sender_signing_public_key()


def get_relay_config() -> RelayConfig:
    """This node's configuration. Overridden per app instance in tests."""
    return RelayConfig.from_settings()


def get_http_client() -> httpx.AsyncClient:
    """The client used to forward a packet to the next node.

    Overridden in tests to route calls to in-process ASGI apps instead of
    real sockets.
    """
    raise RuntimeError("HTTP client dependency was not configured")


SenderKey = Annotated[Ed25519PublicKey, Depends(get_sender_public_key)]
NodeConfig = Annotated[RelayConfig, Depends(get_relay_config)]
HttpClient = Annotated[httpx.AsyncClient, Depends(get_http_client)]


# --- Application -------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(service=SERVICE_NAME)
    client = httpx.AsyncClient(timeout=settings.mesh_forward_timeout_seconds)
    app.state.http_client = client
    logger.info(
        "mesh_relay.startup",
        node_id=settings.mesh_node_id,
        hop_count_target=settings.mesh_hop_count,
        hop_limit=settings.mesh_hop_limit,
    )
    try:
        yield
    finally:
        await client.aclose()
        logger.info("mesh_relay.shutdown")


def _error_response(code: ErrorCode, detail: str, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content=ErrorResponse(code=code, detail=detail).model_dump(mode="json"),
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="MeshSettle Mesh Relay",
        description=(
            "Simulates a device-to-device hop. Carries packets without "
            "reading or altering them. Simulation only; moves no real money."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # Per-app limiter: a shared module-level one would register a duplicate
    # limit on every create_app() call and shrink the real budget.
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter

    def _client_from_state() -> httpx.AsyncClient:
        return app.state.http_client

    app.dependency_overrides[get_http_client] = _client_from_state

    @app.exception_handler(RelayError)
    async def _relay_error(_request: Request, exc: RelayError) -> JSONResponse:
        return _error_response(exc.code, exc.detail, exc.http_status)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(_request: Request, exc: RateLimitExceeded) -> JSONResponse:
        logger.warning("mesh_relay.rate_limited", limit=str(exc.detail))
        return _error_response(
            ErrorCode.INTERNAL_ERROR,
            "rate limit exceeded",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning(
            "mesh_relay.packet_rejected",
            reason=ErrorCode.MALFORMED_PAYLOAD.value,
        )
        return _error_response(
            ErrorCode.MALFORMED_PAYLOAD,
            _first_validation_message(exc),
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.get("/healthz", tags=["ops"])
    async def healthz(config: NodeConfig) -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME, "node_id": config.node_id}

    @app.post(
        "/relay",
        response_model=RelayResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["mesh"],
        responses={
            400: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    @limiter.limit(f"{settings.rate_limit_per_minute}/minute")
    async def relay(
        request: Request,  # noqa: ARG001 - required by the rate limiter
        packet: PaymentPacket,
        sender_key: SenderKey,
        config: NodeConfig,
        client: HttpClient,
    ) -> RelayResponse:
        """Accept a packet, record the hop, and forward it onward."""
        # 1. Verify before anything else touches the payload.
        try:
            packet.verify(sender_key)
        except SignatureVerificationError as exc:
            logger.warning(
                "mesh_relay.packet_rejected",
                node_id=config.node_id,
                packet_id=str(packet.packet_id),
                reason=ErrorCode.INVALID_SIGNATURE.value,
                signature_fp=safe_fingerprint(packet.signature),
            )
            raise RelayError(
                ErrorCode.INVALID_SIGNATURE,
                "packet signature did not verify",
                status.HTTP_400_BAD_REQUEST,
            ) from exc

        # 2. Loop protection. A packet that has been around the ring too many
        #    times is dropped rather than forwarded again.
        if packet.hop_count >= config.hop_limit:
            logger.warning(
                "mesh_relay.packet_rejected",
                node_id=config.node_id,
                packet_id=str(packet.packet_id),
                reason="HOP_LIMIT_EXCEEDED",
                hop_count=packet.hop_count,
            )
            raise RelayError(
                ErrorCode.MALFORMED_PAYLOAD,
                f"packet exceeded the hop limit of {config.hop_limit}",
                status.HTTP_400_BAD_REQUEST,
            )

        # 3. Record this hop. Unsigned metadata, appended to a copy; the
        #    signed region is untouched, so the signature stays valid.
        forwarded = packet.append_hop(
            HopRecord(node_id=config.node_id, received_at=datetime.now(UTC))
        )

        # 4. Forward onward, byte-for-byte.
        destination, next_status = config.next_destination(forwarded.hop_count)
        body = forwarded.model_dump_json()

        try:
            response = await client.post(
                destination,
                content=body,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "mesh_relay.forward_failed",
                node_id=config.node_id,
                packet_id=str(forwarded.packet_id),
                destination=destination,
                error=type(exc).__name__,
            )
            raise RelayError(
                ErrorCode.INTERNAL_ERROR,
                "could not forward the packet to the next node",
                status.HTTP_502_BAD_GATEWAY,
            ) from exc

        logger.info(
            "mesh_relay.packet_forwarded",
            node_id=config.node_id,
            packet_id=str(forwarded.packet_id),
            hop_count=forwarded.hop_count,
            destination=destination,
        )
        return RelayResponse(
            status=next_status,
            node_id=config.node_id,
            hop_count=forwarded.hop_count,
            forwarded_to=destination,
            packet_id=forwarded.packet_id,
        )

    return app


def _first_validation_message(exc: RequestValidationError) -> str:
    """Summarize a validation failure without echoing submitted values back."""
    errors = exc.errors()
    if not errors:
        return "request body failed validation"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    reason = first.get("msg", "invalid value")
    return f"{location or 'body'}: {reason}"


app = create_app()
