"""Test harness for wiring mesh nodes together in-process.

The relay forwards packets by making a real HTTP request to an absolute URL.
To exercise a multi-hop chain without binding sockets, this module provides a
transport that dispatches by URL prefix to in-process ASGI apps.

That keeps the integration test honest: the packet really is serialized to
JSON, sent as an HTTP request body, parsed and re-validated at the next node,
exactly as it would be across the network. Only the socket is elided.
"""

from __future__ import annotations

import httpx


class RoutingTransport(httpx.AsyncBaseTransport):
    """Dispatch requests to in-process ASGI apps based on URL prefix."""

    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]) -> None:
        # Longest prefix first, so a more specific route wins.
        self._routes = sorted(routes.items(), key=lambda item: len(item[0]), reverse=True)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for prefix, transport in self._routes:
            if url.startswith(prefix):
                return await transport.handle_async_request(request)
        raise httpx.ConnectError(f"no route configured for {url}", request=request)


def asgi_route(app: object) -> httpx.ASGITransport:
    """Wrap an ASGI app as an httpx transport."""
    return httpx.ASGITransport(app=app)  # type: ignore[arg-type]


def mesh_client(routes: dict[str, object]) -> httpx.AsyncClient:
    """Build an AsyncClient that routes absolute URLs to in-process apps.

    Args:
        routes: mapping of URL prefix (e.g. ``"http://relay-1"``) to ASGI app.
    """
    transports = {prefix: asgi_route(app) for prefix, app in routes.items()}
    return httpx.AsyncClient(transport=RoutingTransport(transports))
