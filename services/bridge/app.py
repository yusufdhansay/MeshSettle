"""Bridge node: the first device in the chain that has connectivity.

This is where the offline world ends. The bridge takes a packet that has
travelled hop to hop and puts it on a durable queue. It does not settle
anything, and it deliberately does not call the settlement service directly:
real mesh delivery is asynchronous and unreliable, so the handoff has to be
a queue that survives the settlement service being down, slow, or restarting.

Like the relay, the bridge verifies the signature before doing anything with
the payload, and cannot read the payment itself.

Note: no ``from __future__ import annotations`` here; the rate-limit
decorator wraps route functions and FastAPI would resolve string annotations
against the wrapper's module globals.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from services.bridge.publisher import Publisher, PublishError, RabbitMQPublisher
from shared.config import settings
from shared.crypto import SignatureVerificationError, sender_signing_public_key
from shared.logging import configure_logging, get_logger, safe_fingerprint
from shared.models import (
    BridgeResponse,
    ErrorCode,
    ErrorResponse,
    PacketStatus,
    PaymentPacket,
)

SERVICE_NAME = "bridge"

logger = get_logger(SERVICE_NAME)


class BridgeError(Exception):
    """A packet the bridge refuses to enqueue."""

    def __init__(self, code: ErrorCode, detail: str, http_status: int) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.http_status = http_status


# --- Dependencies ------------------------------------------------------------


def get_sender_public_key() -> Ed25519PublicKey:
    """The sender's verification key, so forged packets never reach the queue."""
    return sender_signing_public_key()


def get_publisher() -> Publisher:
    """The queue publisher. Overridden in tests with an in-memory stand-in."""
    raise RuntimeError("publisher dependency was not configured")


SenderKey = Annotated[Ed25519PublicKey, Depends(get_sender_public_key)]
QueuePublisher = Annotated[Publisher, Depends(get_publisher)]


# --- Application -------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(service=SERVICE_NAME)
    publisher = RabbitMQPublisher()
    await publisher.connect()
    app.state.publisher = publisher
    logger.info("bridge.startup", queue=publisher.queue_name)
    try:
        yield
    finally:
        await publisher.close()
        logger.info("bridge.shutdown")


def _error_response(code: ErrorCode, detail: str, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content=ErrorResponse(code=code, detail=detail).model_dump(mode="json"),
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="MeshSettle Bridge",
        description=(
            "Publishes relayed packets to the settlement queue. "
            "Simulation only; moves no real money."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter

    def _publisher_from_state() -> Publisher:
        return app.state.publisher

    app.dependency_overrides[get_publisher] = _publisher_from_state

    @app.exception_handler(BridgeError)
    async def _bridge_error(_request: Request, exc: BridgeError) -> JSONResponse:
        return _error_response(exc.code, exc.detail, exc.http_status)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(_request: Request, exc: RateLimitExceeded) -> JSONResponse:
        logger.warning("bridge.rate_limited", limit=str(exc.detail))
        return _error_response(
            ErrorCode.INTERNAL_ERROR,
            "rate limit exceeded",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("bridge.packet_rejected", reason=ErrorCode.MALFORMED_PAYLOAD.value)
        return _error_response(
            ErrorCode.MALFORMED_PAYLOAD,
            _first_validation_message(exc),
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.post(
        "/bridge",
        response_model=BridgeResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["mesh"],
        responses={
            400: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    @limiter.limit(f"{settings.rate_limit_per_minute}/minute")
    async def bridge(
        request: Request,  # noqa: ARG001 - required by the rate limiter
        packet: PaymentPacket,
        sender_key: SenderKey,
        publisher: QueuePublisher,
    ) -> BridgeResponse:
        """Verify a relayed packet and publish it to the settlement queue."""
        try:
            packet.verify(sender_key)
        except SignatureVerificationError as exc:
            logger.warning(
                "bridge.packet_rejected",
                packet_id=str(packet.packet_id),
                reason=ErrorCode.INVALID_SIGNATURE.value,
                signature_fp=safe_fingerprint(packet.signature),
            )
            raise BridgeError(
                ErrorCode.INVALID_SIGNATURE,
                "packet signature did not verify",
                status.HTTP_400_BAD_REQUEST,
            ) from exc

        # Re-serialize from the validated model rather than forwarding the raw
        # request body. The signed region is unchanged either way, but this
        # guarantees the consumer receives a canonical, schema-valid document
        # and never an attacker-chosen byte sequence that merely parsed.
        body = packet.model_dump_json().encode("utf-8")
        idempotency_key = packet.idempotency_key

        try:
            await publisher.publish(body, message_id=idempotency_key)
        except PublishError as exc:
            logger.warning(
                "bridge.publish_failed",
                packet_id=str(packet.packet_id),
                queue=publisher.queue_name,
                reason=ErrorCode.INTERNAL_ERROR.value,
            )
            raise BridgeError(
                ErrorCode.INTERNAL_ERROR,
                "packet could not be queued for settlement",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc

        logger.info(
            "bridge.packet_published",
            packet_id=str(packet.packet_id),
            hop_count=packet.hop_count,
            queue=publisher.queue_name,
            idempotency_key=idempotency_key,
        )
        return BridgeResponse(
            status=PacketStatus.BRIDGED,
            queue=publisher.queue_name,
            packet_id=packet.packet_id,
            hop_count=packet.hop_count,
            idempotency_key=idempotency_key,
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
