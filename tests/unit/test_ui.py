"""Tests for the demo UI service.

The UI is not the point of the project, but two things about it are worth
asserting, because getting them wrong would be actively misleading:

* the proxy passes rejections through unchanged. A demo that swallowed a
  DUPLICATE_PACKET response and showed "settled" would misrepresent the exact
  property the project exists to demonstrate.
* the proxy is a fixed allowlist, not a general-purpose forwarder. It takes no
  URL from the client, so it cannot be used to reach arbitrary hosts.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI

from services.ui.app import STATIC_DIR, create_app, get_http_client


class UpstreamStub:
    """Answers the four upstream calls the UI is allowed to make."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, bytes]] = []
        self.responses: dict[str, httpx.Response] = {}

    def set(self, path_fragment: str, response: httpx.Response) -> None:
        self.responses[path_fragment] = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, str(request.url), request.content))
        for fragment, response in self.responses.items():
            if fragment in str(request.url):
                return response
        return httpx.Response(404, json={"code": "MALFORMED_PAYLOAD", "detail": "no stub"})


def _ui_app(upstream: UpstreamStub) -> FastAPI:
    app = create_app()
    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(upstream.handler)
    )
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ui")


# --- Static page -------------------------------------------------------------


def test_static_assets_exist() -> None:
    """The page, its styles and its script must ship with the service."""
    for name in ("index.html", "styles.css", "app.js"):
        assert (STATIC_DIR / name).is_file(), f"{name} is missing"


async def test_index_is_served() -> None:
    upstream = UpstreamStub()
    async with await _client(_ui_app(upstream)) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "MeshSettle" in response.text
    assert "text/html" in response.headers["content-type"]


async def test_healthz() -> None:
    upstream = UpstreamStub()
    async with await _client(_ui_app(upstream)) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ui"}


# --- Proxy behaviour ---------------------------------------------------------


async def test_create_packet_is_proxied_to_the_sender() -> None:
    upstream = UpstreamStub()
    upstream.set(
        "/packets",
        httpx.Response(
            201,
            json={
                "packet": {"packet_id": "abc"},
                "idempotency_key": "settle:deadbeef",
                "status": "created",
            },
        ),
    )

    async with await _client(_ui_app(upstream)) as client:
        response = await client.post(
            "/api/packets",
            json={"payer_id": "device-alice", "payee_id": "device-bob", "amount_minor": 100},
        )

    assert response.status_code == 201
    assert response.json()["idempotency_key"] == "settle:deadbeef"

    method, url, body = upstream.requests[0]
    assert method == "POST"
    assert url.endswith("/packets")
    assert json.loads(body)["amount_minor"] == 100


async def test_relay_forwards_the_packet_bytes_unchanged() -> None:
    """The replay demonstration depends on the same bytes going out again."""
    upstream = UpstreamStub()
    upstream.set("/relay", httpx.Response(202, json={"status": "bridged", "hop_count": 2}))
    packet = {"packet_id": "abc", "signature": "zzz"}

    async with await _client(_ui_app(upstream)) as client:
        response = await client.post("/api/relay", json=packet)

    assert response.status_code == 202
    _method, _url, body = upstream.requests[0]
    assert json.loads(body) == packet


async def test_duplicate_rejection_is_passed_through_verbatim() -> None:
    """A rejection must reach the page intact, code and all."""
    upstream = UpstreamStub()
    upstream.set(
        "/relay",
        httpx.Response(400, json={"code": "INVALID_SIGNATURE", "detail": "nope"}),
    )

    async with await _client(_ui_app(upstream)) as client:
        response = await client.post("/api/relay", json={"packet_id": "abc"})

    assert response.status_code == 400
    assert response.json() == {"code": "INVALID_SIGNATURE", "detail": "nope"}


async def test_settlement_lookup_is_proxied() -> None:
    upstream = UpstreamStub()
    upstream.set(
        "/settlements/",
        httpx.Response(200, json={"id": 1, "amount_minor": 4567, "hop_count": 2}),
    )

    async with await _client(_ui_app(upstream)) as client:
        response = await client.get("/api/settlements/settle:abc123")

    assert response.status_code == 200
    assert response.json()["amount_minor"] == 4567
    assert "settle:abc123" in upstream.requests[0][1]


async def test_missing_settlement_returns_404_to_the_page() -> None:
    """The page polls on 404 while a packet is in flight, so it must survive."""
    upstream = UpstreamStub()
    upstream.set(
        "/settlements/",
        httpx.Response(404, json={"code": "MALFORMED_PAYLOAD", "detail": "not found"}),
    )

    async with await _client(_ui_app(upstream)) as client:
        response = await client.get("/api/settlements/settle:missing")

    assert response.status_code == 404


async def test_metrics_are_proxied() -> None:
    upstream = UpstreamStub()
    upstream.set(
        "/metrics",
        httpx.Response(
            200,
            json={
                "settled": 3,
                "rejected": 1,
                "duplicates": 1,
                "invalid_signature": 0,
                "malformed": 0,
                "decryption_failed": 0,
                "internal_errors": 0,
                "queue_depth": 0,
            },
        ),
    )

    async with await _client(_ui_app(upstream)) as client:
        response = await client.get("/api/metrics")

    assert response.status_code == 200
    assert response.json()["settled"] == 3


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/packets"),
        ("POST", "/api/relay"),
        ("GET", "/api/metrics"),
        ("GET", "/api/settlements/settle:abc"),
    ],
)
async def test_upstream_failure_becomes_a_bad_gateway(method: str, path: str) -> None:
    """An unreachable upstream must not surface as a fake success."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down", request=request)

    app = create_app()
    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(refuse)
    )

    async with await _client(app) as client:
        response = await client.request(method, path, json={} if method == "POST" else None)

    assert response.status_code == 502
    assert response.json()["code"] == "INTERNAL_ERROR"


# --- The proxy is not a general-purpose forwarder ----------------------------


async def test_proxy_exposes_no_client_controlled_url() -> None:
    """There must be no route that forwards to a URL supplied by the caller."""
    app = create_app()
    paths = {route.path for route in app.routes}

    assert paths == {
        "/openapi.json",
        "/healthz",
        "/",
        "/api/packets",
        "/api/relay",
        "/api/metrics",
        "/api/settlements/{idempotency_key}",
        "/static",
    }, f"unexpected route surface: {sorted(paths)}"


async def test_no_write_path_to_settlement_is_exposed() -> None:
    """Settlement is queue-only; the UI must not offer a way to write to it."""
    upstream = UpstreamStub()
    async with await _client(_ui_app(upstream)) as client:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            response = await client.request(method, "/api/settlements/settle:abc")
            assert response.status_code in {404, 405}


# --- DESIGN.md compliance ----------------------------------------------------
#
# DESIGN.md is specific about the visual language, and "clean and boring on
# purpose" is easy to drift away from. These assert the constraints that are
# actually checkable from the source: the exact palette, the three-font-size
# limit, no shadows or gradients, the stepper stages, and monospace for hashes.


def _css() -> str:
    """The stylesheet with comments stripped.

    Comments are removed because they describe the rules ("no gradients",
    "never a large badge"), and a naive substring check would match the prose
    rather than the declarations it is describing.
    """
    import re

    raw = (STATIC_DIR / "styles.css").read_text()
    return re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("background", "#FAFAFA"),
        ("dark background", "#0A0A0A"),
        ("primary text", "#171717"),
        ("muted text", "#737373"),
        ("border", "#E5E5E5"),
        ("settled green", "#16A34A"),
        ("pending amber", "#D97706"),
        ("rejected red", "#DC2626"),
    ],
)
def test_palette_matches_design_doc(name: str, value: str) -> None:
    assert value in _css(), f"DESIGN.md {name} {value} is not used"


def test_no_shadows() -> None:
    """DESIGN.md: flat cards, no drop shadows."""
    assert "box-shadow" not in _css()


def test_no_gradients() -> None:
    """DESIGN.md: no gradients anywhere."""
    css = _css()
    assert "gradient" not in css


def test_exactly_three_font_sizes() -> None:
    """DESIGN.md: no more than 3 font sizes on any single screen."""
    css = _css()
    declared = [line for line in css.splitlines() if line.strip().startswith("--size-")]
    assert len(declared) == 3, f"expected 3 font-size tokens, found {declared}"

    # And no ad-hoc px font sizes bypassing those tokens.
    import re

    literals = re.findall(r"font-size:\s*(\d+)px", css)
    assert not literals, f"font sizes should come from the tokens, found {literals}"


def test_cards_use_an_eight_pixel_radius() -> None:
    """DESIGN.md: 8px corner radius on cards."""
    assert "--radius: 8px" in _css()


def test_status_is_a_small_dot_not_a_badge() -> None:
    """DESIGN.md: status is a small coloured dot plus a label, not a large badge."""
    import re

    css = _css()
    # The base `.dot` rule, not a descendant selector like `.step.is-done .dot`.
    match = re.search(r"(?m)^\.dot\s*\{(.*?)\}", css, flags=re.DOTALL)
    assert match, "no base .dot rule found"
    dot_block = match.group(1)
    assert "width: 8px" in dot_block
    assert "border-radius: 50%" in dot_block
    assert "badge" not in css.lower()


def test_stepper_has_the_four_documented_stages() -> None:
    """DESIGN.md: Created -> Relayed -> Bridged -> Settled/Rejected."""
    html = _html()
    for stage in ("Created", "Relayed", "Bridged", "Settled"):
        assert stage in html, f"stepper stage {stage} is missing"
    assert 'data-step="created"' in html
    assert 'data-step="relayed"' in html
    assert 'data-step="bridged"' in html
    assert 'data-step="final"' in html


def test_hashes_and_ids_use_a_monospace_class() -> None:
    """DESIGN.md: monospace for anything showing a hash, signature or packet ID."""
    assert "monospace" in _css()
    html = _html()
    # The packet id and idempotency key are the two hash-like values on screen.
    for element_id in ("fact-packet-id", "fact-idempotency-key"):
        marker = f'class="mono" id="{element_id}"'
        assert marker in html, f"{element_id} should be monospace"


def test_system_font_stack_is_used() -> None:
    """DESIGN.md: system font stack."""
    css = _css()
    assert "-apple-system" in css
    assert "BlinkMacSystemFont" in css


def test_only_two_font_weights() -> None:
    """DESIGN.md: one weight for body (400), one for headings/emphasis (600)."""
    import re

    weights = set(re.findall(r"font-weight:\s*(\d+)", _css()))
    assert weights <= {"400", "600"}, f"unexpected font weights: {weights}"


def test_page_states_it_is_not_affiliated_with_any_real_network() -> None:
    """The PRD is explicit about this, so the UI should not imply otherwise."""
    html = _html()
    assert "Not affiliated" in html
    assert "no real money" in html


def test_buttons_are_solid_primary_and_outline_secondary() -> None:
    """DESIGN.md: solid fill for primary, outline for secondary."""
    css = _css()
    assert ".btn-primary" in css
    assert ".btn-outline" in css
    outline_block = css.split(".btn-outline {", 1)[1].split("}", 1)[0]
    assert "background: transparent" in outline_block


def test_reduced_motion_is_respected() -> None:
    """DESIGN.md wants restrained motion; honour the OS preference too."""
    assert "prefers-reduced-motion" in _css()
