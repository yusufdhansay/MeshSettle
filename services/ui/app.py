"""Demo UI: serves the static page and proxies the few calls it needs.

Why this is its own service rather than static files bolted onto the sender:

* The sender represents a payer's *device*. Giving it a web UI and a
  settlement read-proxy would blur what it models.
* Serving the page from the same origin as its API calls avoids CORS
  entirely. The alternative was adding permissive CORS middleware to the
  sender and settlement services, which loosens two payment-path services
  for the benefit of a demo page. Not a trade worth making.

The proxy is deliberately a fixed, tiny allowlist of four upstream calls. It
takes no URL from the client, so it cannot be turned into an open proxy into
the cluster network.

Note: no ``from __future__ import annotations`` here, to stay consistent with
the other service modules (FastAPI plus decorators resolve real annotations).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from shared.config import settings
from shared.logging import configure_logging, get_logger
from shared.models import ErrorCode, ErrorResponse

SERVICE_NAME = "ui"

logger = get_logger(SERVICE_NAME)

STATIC_DIR = Path(__file__).parent / "static"

#: Upstream timeout for a proxied call. Generous enough for a packet to make
#: two mesh hops and reach the broker.
UPSTREAM_TIMEOUT_SECONDS = 15.0


def get_http_client() -> httpx.AsyncClient:
    """Client used for upstream calls. Overridden in tests."""
    raise RuntimeError("HTTP client dependency was not configured")


HttpClient = Annotated[httpx.AsyncClient, Depends(get_http_client)]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(service=SERVICE_NAME)
    client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)
    app.state.http_client = client
    logger.info(
        "ui.startup",
        sender_url=settings.sender_url,
        settlement_url=settings.settlement_url,
    )
    try:
        yield
    finally:
        await client.aclose()
        logger.info("ui.shutdown")


def _bad_gateway(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=ErrorResponse(code=ErrorCode.INTERNAL_ERROR, detail=detail).model_dump(mode="json"),
    )


def _passthrough(upstream: httpx.Response) -> Response:
    """Return the upstream response verbatim.

    The UI needs the real status code and body, including rejections, because
    showing a duplicate or an invalid signature honestly is the entire point of
    the demo. Nothing is rewritten or prettified here.
    """
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="MeshSettle Demo UI",
        description=(
            "Minimal viewer for a packet's journey. Simulation only; " "moves no real money."
        ),
        version="0.1.0",
        lifespan=lifespan,
        # No interactive API docs: this is a static page host with a four-call
        # proxy, and its upstreams publish their own OpenAPI.
        docs_url=None,
        redoc_url=None,
    )

    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter

    def _client_from_state() -> httpx.AsyncClient:
        return app.state.http_client

    app.dependency_overrides[get_http_client] = _client_from_state

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limited(_request: Request, exc: RateLimitExceeded) -> JSONResponse:
        logger.warning("ui.rate_limited", limit=str(exc.detail))
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=ErrorResponse(
                code=ErrorCode.INTERNAL_ERROR, detail="rate limit exceeded"
            ).model_dump(mode="json"),
        )

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    # --- Proxy: create a packet without submitting it -------------------------

    @app.post("/api/packets")
    @limiter.limit(f"{settings.rate_limit_per_minute}/minute")
    async def create_packet(request: Request, client: HttpClient) -> Response:
        """Mint a signed packet and hand it back, so the page can replay it.

        The page needs the packet itself to demonstrate deduplication: sending
        the *same* packet twice is what proves exactly-once, and a fresh
        packet each time would just be two different payments.
        """
        body: Any = await request.json()
        try:
            upstream = await client.post(
                f"{settings.sender_url.rstrip('/')}/packets",
                json=body,
            )
        except httpx.HTTPError:
            logger.warning("ui.upstream_failed", upstream="sender", path="/packets")
            return _bad_gateway("the sender service is unreachable")
        return _passthrough(upstream)

    # --- Proxy: hand an existing packet to the mesh ---------------------------

    @app.post("/api/relay")
    @limiter.limit(f"{settings.rate_limit_per_minute}/minute")
    async def relay_packet(request: Request, client: HttpClient) -> Response:
        """Submit a packet the page already holds into the mesh."""
        raw = await request.body()
        try:
            upstream = await client.post(
                f"{settings.mesh_relay_url.rstrip('/')}/relay",
                content=raw,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError:
            logger.warning("ui.upstream_failed", upstream="mesh_relay", path="/relay")
            return _bad_gateway("the mesh relay is unreachable")
        return _passthrough(upstream)

    # --- Proxy: settlement reads ----------------------------------------------

    @app.get("/api/settlements/{idempotency_key}")
    @limiter.limit(f"{settings.rate_limit_per_minute}/minute")
    async def read_settlement(
        request: Request, idempotency_key: str, client: HttpClient
    ) -> Response:
        """Look up one settlement. Polled by the page while a packet is in flight."""
        try:
            upstream = await client.get(
                f"{settings.settlement_url.rstrip('/')}/settlements/{idempotency_key}"
            )
        except httpx.HTTPError:
            logger.warning("ui.upstream_failed", upstream="settlement", path="/settlements")
            return _bad_gateway("the settlement service is unreachable")
        return _passthrough(upstream)

    @app.get("/api/metrics")
    @limiter.limit(f"{settings.rate_limit_per_minute}/minute")
    async def read_metrics(request: Request, client: HttpClient) -> Response:
        """Settlement counters, shown as a small strip on the page."""
        try:
            upstream = await client.get(f"{settings.settlement_url.rstrip('/')}/metrics")
        except httpx.HTTPError:
            logger.warning("ui.upstream_failed", upstream="settlement", path="/metrics")
            return _bad_gateway("the settlement service is unreachable")
        return _passthrough(upstream)

    # --- Static page ----------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


app = create_app()
