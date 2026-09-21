"""Unit tests for the sender service HTTP surface.

Keys are injected via FastAPI dependency overrides so these tests never
depend on environment-configured key material.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from services.sender.app import (
    create_app,
    get_recipient_public_key,
    get_signing_key,
    sender_id_for,
)
from shared.models import ErrorCode, PaymentPacket


@pytest.fixture
def client(keys) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_signing_key] = lambda: keys.signing_private
    app.dependency_overrides[get_recipient_public_key] = lambda: keys.rsa_public
    with TestClient(app) as test_client:
        yield test_client


def _valid_body(**overrides) -> dict[str, object]:
    body: dict[str, object] = {
        "payer_id": "device-alice",
        "payee_id": "device-bob",
        "amount_minor": 125_00,
        "currency": "INR",
    }
    body.update(overrides)
    return body


# --- Health ------------------------------------------------------------------


def test_healthz_reports_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "sender"}


# --- Packet creation ---------------------------------------------------------


def test_create_packet_returns_a_verifiable_packet(client: TestClient, keys) -> None:
    response = client.post("/packets", json=_valid_body())
    assert response.status_code == 201

    body = response.json()
    packet = PaymentPacket.model_validate(body["packet"])

    packet.verify(keys.signing_public)
    assert body["status"] == "created"
    assert body["idempotency_key"] == packet.idempotency_key
    assert packet.hop_count == 0


def test_created_packet_carries_the_requested_instruction(client: TestClient, keys) -> None:
    response = client.post(
        "/packets", json=_valid_body(amount_minor=77_77, payee_id="device-carol")
    )
    packet = PaymentPacket.model_validate(response.json()["packet"])
    instruction = packet.open_instruction(keys.rsa_private)

    assert instruction.amount_minor == 77_77
    assert instruction.payee_id == "device-carol"
    assert instruction.payer_id == "device-alice"
    assert instruction.packet_id == packet.packet_id


def test_sender_id_is_derived_from_the_signing_key(client: TestClient, keys) -> None:
    response = client.post("/packets", json=_valid_body())
    packet = PaymentPacket.model_validate(response.json()["packet"])
    assert packet.sender_id == sender_id_for(keys.signing_private)


def test_each_request_produces_a_distinct_packet(client: TestClient) -> None:
    """Two identical requests are two different payments, not a duplicate."""
    first = client.post("/packets", json=_valid_body()).json()
    second = client.post("/packets", json=_valid_body()).json()

    assert first["packet"]["packet_id"] != second["packet"]["packet_id"]
    assert first["idempotency_key"] != second["idempotency_key"]


def test_response_does_not_leak_the_instruction(client: TestClient) -> None:
    """The payee and amount must not be readable from the response body."""
    raw = client.post("/packets", json=_valid_body(payee_id="device-carol")).text
    assert "device-carol" not in raw
    assert "1250" not in raw


def test_response_does_not_contain_key_material(client: TestClient) -> None:
    raw = client.post("/packets", json=_valid_body()).text
    assert "BEGIN PRIVATE KEY" not in raw
    assert "PRIVATE" not in raw


# --- Input validation --------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"payer_id": "device-alice"},
        _valid_body(amount_minor=0),
        _valid_body(amount_minor=-100),
        _valid_body(amount_minor="lots"),
        _valid_body(amount_minor=12.5),
        _valid_body(payer_id=""),
        _valid_body(payer_id="has spaces"),
        _valid_body(payee_id="device-alice"),  # same as payer
        _valid_body(currency="inr"),
        _valid_body(currency="RUPEES"),
        _valid_body(unexpected="field"),
    ],
)
def test_invalid_requests_are_rejected_as_malformed(
    client: TestClient, body: dict[str, object]
) -> None:
    response = client.post("/packets", json=body)
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.MALFORMED_PAYLOAD.value


def test_validation_error_does_not_echo_submitted_values(client: TestClient) -> None:
    """Error text should name the field, not repeat the input back."""
    response = client.post("/packets", json=_valid_body(payer_id="secret-looking-value!!"))
    assert response.status_code == 422
    assert "secret-looking-value" not in response.text


def test_non_json_body_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/packets",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.MALFORMED_PAYLOAD.value


def test_error_body_uses_the_shared_error_shape(client: TestClient) -> None:
    body = client.post("/packets", json={}).json()
    assert set(body) == {"code", "detail"}
    assert body["code"] in {code.value for code in ErrorCode}


# --- Rate limiting -----------------------------------------------------------


def test_packet_endpoint_is_rate_limited(keys) -> None:
    """A public endpoint must shed load rather than accept unbounded traffic."""
    app = create_app()
    app.dependency_overrides[get_signing_key] = lambda: keys.signing_private
    app.dependency_overrides[get_recipient_public_key] = lambda: keys.rsa_public

    with TestClient(app) as client:
        statuses = [client.post("/packets", json=_valid_body()).status_code for _ in range(200)]

    assert 429 in statuses, "expected the limiter to reject some requests"
    first_limited = statuses.index(429)
    assert first_limited > 0, "expected some requests to succeed before limiting"
    assert all(code == 201 for code in statuses[:first_limited])
    assert first_limited == 60, f"expected the configured 60/minute budget, got {first_limited}"


def test_rate_limited_response_uses_the_shared_error_shape(keys) -> None:
    app = create_app()
    app.dependency_overrides[get_signing_key] = lambda: keys.signing_private
    app.dependency_overrides[get_recipient_public_key] = lambda: keys.rsa_public

    with TestClient(app) as client:
        response = None
        for _ in range(200):
            response = client.post("/packets", json=_valid_body())
            if response.status_code == 429:
                break

    assert response is not None and response.status_code == 429
    assert set(response.json()) == {"code", "detail"}
    assert response.json()["detail"] == "rate limit exceeded"


def test_healthz_is_not_rate_limited(client: TestClient) -> None:
    statuses = {client.get("/healthz").status_code for _ in range(150)}
    assert statuses == {200}


# --- OpenAPI -----------------------------------------------------------------


def test_openapi_documents_the_error_shape(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/packets"]["post"]["responses"]
    assert "422" in responses
    assert "429" in responses
    assert json.dumps(schema).count("ErrorResponse") > 0


# --- Handing a packet to the mesh -------------------------------------------


def _submit_client(keys, handler) -> TestClient:
    """A sender whose mesh handoff goes to a stub instead of a real relay."""
    import httpx

    from services.sender.app import get_http_client, get_mesh_entrypoint

    app = create_app()
    app.dependency_overrides[get_signing_key] = lambda: keys.signing_private
    app.dependency_overrides[get_recipient_public_key] = lambda: keys.rsa_public
    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    app.dependency_overrides[get_mesh_entrypoint] = lambda: "http://relay-1/relay"
    return TestClient(app)


class RelayStub:
    """Captures what the sender handed to the mesh."""

    def __init__(self, status_code: int = 202, body: dict | None = None) -> None:
        self.status_code = status_code
        self.body = body if body is not None else {"status": "relayed", "hop_count": 1}
        self.requests: list[bytes] = []

    def __call__(self, request):  # noqa: ANN001 - httpx passes its own Request type
        import httpx

        self.requests.append(request.content)
        return httpx.Response(self.status_code, json=self.body)


def test_submit_creates_a_packet_and_hands_it_to_the_mesh(keys) -> None:
    relay = RelayStub()
    with _submit_client(keys, relay) as client:
        response = client.post("/packets/submit", json=_valid_body())

    assert response.status_code == 202
    body = response.json()
    assert body["submitted_to"] == "http://relay-1/relay"
    assert body["status"] == "relayed"
    assert body["hop_count"] == 1

    # Exactly one packet was handed over, and it verifies.
    assert len(relay.requests) == 1
    handed = PaymentPacket.model_validate_json(relay.requests[0])
    handed.verify(keys.signing_public)
    assert str(handed.packet_id) == body["packet_id"]
    assert handed.idempotency_key == body["idempotency_key"]
    assert handed.hop_count == 0, "the sender records no hops of its own"


def test_submitted_packet_carries_the_requested_instruction(keys) -> None:
    relay = RelayStub()
    with _submit_client(keys, relay) as client:
        client.post("/packets/submit", json=_valid_body(amount_minor=333_33))

    handed = PaymentPacket.model_validate_json(relay.requests[0])
    instruction = handed.open_instruction(keys.rsa_private)
    assert instruction.amount_minor == 333_33


def test_submit_reports_bridged_when_the_mesh_says_so(keys) -> None:
    relay = RelayStub(body={"status": "bridged", "hop_count": 2})
    with _submit_client(keys, relay) as client:
        body = client.post("/packets/submit", json=_valid_body()).json()

    assert body["status"] == "bridged"
    assert body["hop_count"] == 2


def test_submit_reports_bad_gateway_when_no_mesh_node_accepts(keys) -> None:
    """If the handoff fails the caller must not be told the payment is on its way."""
    relay = RelayStub(status_code=503, body={"code": "INTERNAL_ERROR", "detail": "nope"})
    with _submit_client(keys, relay) as client:
        response = client.post("/packets/submit", json=_valid_body())

    assert response.status_code == 502
    assert response.json()["code"] == ErrorCode.INTERNAL_ERROR.value


def test_submit_reports_bad_gateway_when_the_mesh_is_unreachable(keys) -> None:
    import httpx

    def refuse(request):  # noqa: ANN001, ANN202
        raise httpx.ConnectError("no neighbour in range", request=request)

    with _submit_client(keys, refuse) as client:
        response = client.post("/packets/submit", json=_valid_body())

    assert response.status_code == 502
    assert response.json()["code"] == ErrorCode.INTERNAL_ERROR.value


def test_submit_validates_input_before_any_handoff(keys) -> None:
    relay = RelayStub()
    with _submit_client(keys, relay) as client:
        response = client.post("/packets/submit", json=_valid_body(amount_minor=-1))

    assert response.status_code == 422
    assert response.json()["code"] == ErrorCode.MALFORMED_PAYLOAD.value
    assert relay.requests == [], "an invalid request must not reach the mesh"


def test_submit_does_not_leak_the_instruction_in_its_response(keys) -> None:
    relay = RelayStub()
    with _submit_client(keys, relay) as client:
        raw = client.post("/packets/submit", json=_valid_body(payee_id="device-carol")).text

    assert "device-carol" not in raw
    assert "1250" not in raw


def test_each_submit_creates_a_distinct_packet(keys) -> None:
    relay = RelayStub()
    with _submit_client(keys, relay) as client:
        first = client.post("/packets/submit", json=_valid_body()).json()
        second = client.post("/packets/submit", json=_valid_body()).json()

    assert first["packet_id"] != second["packet_id"]
    assert first["idempotency_key"] != second["idempotency_key"]
