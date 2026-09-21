"""Sender service: turns a transaction request into a signed, sealed packet.

This stands in for the payer's device. In the real world it would have no
internet connection at this moment, which is why it produces a
self-contained, independently verifiable artifact rather than calling an
API: the packet has to survive an untrusted, store-and-forward journey.

The service holds the sender's Ed25519 signing key and the settlement
service's RSA public key. It never sees settlement's private key, so it
cannot read back a packet it has sealed.

Note: this module deliberately does not use ``from __future__ import
annotations``. The rate-limit decorator wraps the route function, and
FastAPI resolves string annotations against the wrapper's module globals,
where our model names do not exist. Real annotation objects avoid that.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from shared.config import settings
from shared.crypto import (
    CryptoError,
    sender_signing_private_key,
    settlement_rsa_public_key,
)
from shared.logging import configure_logging, get_logger, safe_fingerprint
from shared.models import (
    ErrorCode,
    ErrorResponse,
    PacketCreatedResponse,
    PaymentPacket,
    TransactionRequest,
)

SERVICE_NAME = "sender"

logger = get_logger(SERVICE_NAME)


class PacketError(Exception):
    """A request that must be rejected with a specific error code."""

    def __init__(self, code: ErrorCode, detail: str, http_status: int) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.http_status = http_status


# --- Key dependencies --------------------------------------------------------
#
# Injected rather than imported at call sites so tests can supply ephemeral
# keys without touching the environment. In production these resolve to the
# cached, env-backed keys that persist across restarts.


def get_signing_key() -> Ed25519PrivateKey:
    """The sender's Ed25519 signing key."""
    return sender_signing_private_key()


def get_recipient_public_key() -> rsa.RSAPublicKey:
    """Settlement's RSA public key, used to wrap each packet's content key."""
    return settlement_rsa_public_key()


SigningKey = Annotated[Ed25519PrivateKey, Depends(get_signing_key)]
RecipientKey = Annotated[rsa.RSAPublicKey, Depends(get_recipient_public_key)]


def sender_id_for(signing_key: Ed25519PrivateKey) -> str:
    """Derive this device's logical id from the key that signs its packets.

    Tying the identity to the verification key means a packet's ``sender_id``
    and its signature cannot disagree about who sent it, and the id is stable
    across restarts because the key is persisted.
    """
    raw = signing_key.public_key().public_bytes_raw()
    return f"device-{safe_fingerprint(raw, 8)}"


# --- Application -------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging(service=SERVICE_NAME)
    logger.info("sender.startup", rate_limit_per_minute=settings.rate_limit_per_minute)
    yield
    logger.info("sender.shutdown")


def _error_response(code: ErrorCode, detail: str, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content=ErrorResponse(code=code, detail=detail).model_dump(mode="json"),
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="MeshSettle Sender",
        description=(
            "Creates signed, encrypted offline payment packets. "
            "Simulation only; moves no real money."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # The limiter is per-app, not module-level. slowapi keys its registered
    # limits by endpoint name, so a shared instance would accumulate a
    # duplicate limit on every create_app() call and each request would then
    # consume several hits from the same budget.
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter

    @app.exception_handler(PacketError)
    async def _packet_error(_request: Request, exc: PacketError) -> JSONResponse:
        logger.warning("sender.packet_rejected", reason=exc.code.value)
        return _error_response(exc.code, exc.detail, exc.http_status)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(_request: Request, exc: RateLimitExceeded) -> JSONResponse:
        logger.warning("sender.rate_limited", limit=str(exc.detail))
        return _error_response(
            ErrorCode.INTERNAL_ERROR,
            "rate limit exceeded",
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Malformed input is rejected with a specific code, never half-processed."""
        logger.warning("sender.packet_rejected", reason=ErrorCode.MALFORMED_PAYLOAD.value)
        return _error_response(
            ErrorCode.MALFORMED_PAYLOAD,
            _first_validation_message(exc),
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.post(
        "/packets",
        response_model=PacketCreatedResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["packets"],
        responses={
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
        },
    )
    @limiter.limit(f"{settings.rate_limit_per_minute}/minute")
    async def create_packet(
        request: Request,  # noqa: ARG001 - required by the rate limiter
        body: TransactionRequest,
        signing_key: SigningKey,
        recipient_key: RecipientKey,
    ) -> PacketCreatedResponse:
        """Create a signed, encrypted packet for an offline payment.

        The request body is fully validated by Pydantic before any crypto
        runs. The amount and payee are never logged.
        """
        instruction = body.to_instruction()
        try:
            packet = PaymentPacket.create(
                instruction=instruction,
                sender_id=sender_id_for(signing_key),
                signing_key=signing_key,
                recipient_public_key=recipient_key,
            )
        except CryptoError as exc:
            logger.exception(
                "sender.packet_creation_failed",
                packet_id=str(instruction.packet_id),
            )
            raise PacketError(
                ErrorCode.INTERNAL_ERROR,
                "packet could not be created",
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            ) from exc

        logger.info(
            "sender.packet_created",
            packet_id=str(packet.packet_id),
            sender_id=packet.sender_id,
            idempotency_key=packet.idempotency_key,
            signature_fp=safe_fingerprint(packet.signature),
        )
        return PacketCreatedResponse.of(packet)

    return app


def _first_validation_message(exc: RequestValidationError) -> str:
    """Summarize a validation error without echoing the submitted values back."""
    errors = exc.errors()
    if not errors:
        return "request body failed validation"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    reason = first.get("msg", "invalid value")
    return f"{location or 'body'}: {reason}"


app = create_app()
