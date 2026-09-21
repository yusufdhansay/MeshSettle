# MeshSettle — Build Memory

This file is the persistent context across sessions. Read it first,
every time, before doing anything else.

## Current Phase
Phase 4: Settlement service (the core of the project) — next

## Completed Phases
- Phase 0 — Scaffolding: six root docs, folder tree per ARCHITECTURE.md,
  `pyproject.toml`, `.env.example`, `.gitignore`, `shared/config.py`,
  Python 3.12.9 venv with all deps installed. Lint + format clean.
  Commit `d08f52a` — 2026-09-21
- Phase 1 — Crypto core: `shared/crypto.py` (Ed25519 sign/verify, RSA-OAEP
  key wrapping, AES-256-GCM payload encryption, canonical JSON, AAD
  binding, idempotency key derivation) and `shared/keygen.py`. 53 unit
  tests in `tests/unit/test_crypto.py`, all passing. Lint + format clean.
  Commit `967a30d` — 2026-09-21
- Phase 2 — Packet model and sender service: `shared/models.py` (full
  packet schema), `shared/logging.py` (structlog with secret redaction),
  `services/sender/app.py` (FastAPI, rate limited, dependency-injected
  keys). 136 unit tests passing. Verified the real env-key path by
  running the service and decrypting a live packet.
  Commit `7ea2030` — 2026-09-21
- Phase 3 — Mesh relay and bridge: `services/mesh_relay/app.py`,
  `services/bridge/publisher.py`, `services/bridge/app.py`, plus the
  `tests/mesh_harness.py` routing transport. 182 tests passing, including
  6 against a real RabbitMQ broker in Docker.
  Commit `<phase3>` — 2026-09-21

## In Progress
Nothing in flight. Phase 3 closed, Phase 4 (settlement service) not yet
started.

## Mesh/bridge API reference (for later phases)
`services/mesh_relay/app.py`:
- `create_app()`, `app`, `SERVICE_NAME = "mesh_relay"`
- `RelayConfig(node_id, next_relay_url, bridge_url, hop_count_target,
  hop_limit)`, frozen dataclass, with `.from_settings()` and
  `.next_destination(hop_count) -> (url, PacketStatus)`
- Dependencies to override in tests: `get_sender_public_key`,
  `get_relay_config`, `get_http_client`
- `RelayError(code, detail, http_status)`
- Routes: `GET /healthz` (reports node_id), `POST /relay` → 202
  `RelayResponse`. Rejects: 400 INVALID_SIGNATURE, 400 MALFORMED_PAYLOAD
  (hop limit), 422 MALFORMED_PAYLOAD, 502 INTERNAL_ERROR (next node
  unreachable or rejected the packet).

`services/bridge/publisher.py`:
- `Publisher` Protocol: `async publish(body: bytes, *, message_id: str)`
  plus `queue_name` property
- `RabbitMQPublisher(url=None, queue=None)` with `.connect()`, `.close()`,
  `.publish()`. Uses `connect_robust`, `publisher_confirms=True`, durable
  queue, PERSISTENT delivery mode, default exchange, routing_key = queue.
- `InMemoryPublisher` records `(body, message_id)` tuples in `.messages`
- `PublishError` raised when not connected or the broker did not confirm

`services/bridge/app.py`:
- `create_app()`, `app`, dependencies `get_sender_public_key`,
  `get_publisher`; `BridgeError(code, detail, http_status)`
- Routes: `GET /healthz`, `POST /bridge` → 202 `BridgeResponse`.
  Rejects: 400 INVALID_SIGNATURE, 422 MALFORMED_PAYLOAD, 503
  INTERNAL_ERROR (broker did not confirm).
- Re-serializes from the validated model before publishing, so the
  consumer always receives a canonical schema-valid document.

New `shared/models.py` additions: `RelayResponse(status, node_id,
hop_count, forwarded_to, packet_id)` and `BridgeResponse(status, queue,
packet_id, hop_count, idempotency_key)`.

New config: `MESH_HOP_LIMIT` (default 16), `MESH_NODE_ID` (default
"relay-1"), `MESH_FORWARD_TIMEOUT_SECONDS` (default 5.0).

`tests/mesh_harness.py`: `mesh_client({url_prefix: asgi_app})` returns an
`httpx.AsyncClient` that dispatches absolute URLs to in-process ASGI apps
by longest-prefix match. Use this to chain relay → relay → bridge without
binding sockets.

## Running dependencies locally
Docker Desktop was not running at session start; started it with
`open -a Docker`. RabbitMQ for tests:
`docker run --rm --name meshsettle-rabbit-test --hostname meshrabbit -p 5673:5672 rabbitmq:3.13`
Takes roughly two minutes to become ready; poll with
`docker exec meshsettle-rabbit-test rabbitmq-diagnostics -q ping`.
Do NOT use `rabbitmq:3.13-alpine`: it crashes on startup with
`Error when reading /var/lib/rabbitmq/.erlang.cookie: eacces`. The
official Debian-based image with an explicit `--hostname` works.
Integration tests read `MESHSETTLE_TEST_AMQP_URL` and default to
`amqp://guest:guest@localhost:5673/`; they skip cleanly when no broker is
reachable, so a plain `pytest` run needs no Docker.

## Models API reference (for later phases)
`shared/models.py`:
- `ErrorCode` StrEnum: MALFORMED_PAYLOAD, INVALID_SIGNATURE,
  DUPLICATE_PACKET, DECRYPTION_FAILED, INTERNAL_ERROR
- `PacketStatus` StrEnum: created, relayed, bridged, settled, rejected
- `PaymentInstruction` (frozen, extra=forbid): packet_id, payer_id,
  payee_id, amount_minor (gt 0, le 1e12), currency, created_at.
  `.to_canonical_bytes()` / `.from_canonical_bytes(raw)`
- `EncryptedEnvelope` (frozen): encrypted_key, nonce (must be 12 bytes),
  ciphertext, aad. Bytes serialize as unpadded base64url both ways.
- `HopRecord` (frozen): node_id, received_at
- `PaymentPacket` (frozen): packet_id, sender_id, created_at, envelope,
  signature (must be 64 bytes), hops (tuple, defaults empty).
  - `.signing_bytes` / `.idempotency_key` / `.hop_count` properties
  - `PaymentPacket.create(instruction=, sender_id=, signing_key=,
    recipient_public_key=, created_at=None)`
  - `.append_hop(hop)` returns a NEW packet, signed region untouched
  - `.verify(ed25519_public)` raises `SignatureVerificationError`
  - `.open_instruction(rsa_private)` decrypts AND enforces inner==outer
    packet_id, raises `DecryptionError` or `ValueError`
  - `.expected_aad()` for comparing header-implied AAD to the envelope
- `TransactionRequest` (extra=forbid) with `.to_instruction()`
- `PacketCreatedResponse.of(packet)` → {packet, idempotency_key, status}
- `ErrorResponse`: {code, detail}
- `canonical_timestamp(dt)`, `MAX_AMOUNT_MINOR = 10**12`,
  `IDENTIFIER_PATTERN = ^[A-Za-z0-9_.:\-]{1,64}$`

`shared/logging.py`: `configure_logging(service=, json_output=True)`,
`get_logger(name)`, `safe_fingerprint(raw, length=12)`, plus a redaction
processor that blanks any log field whose name contains password, secret,
token, credential, private_key, pem, etc.

`services/sender/app.py`: `create_app()`, `app`, dependencies
`get_signing_key` / `get_recipient_public_key` (override these in tests),
`sender_id_for(signing_key)`, `PacketError(code, detail, http_status)`.
Routes: `GET /healthz`, `POST /packets` (201, rate limited).

## Crypto API reference (for later phases)
`shared/crypto.py` public surface, so later phases don't re-derive it:
- `generate_signing_keypair() -> (private_pem, public_pem)` (Ed25519)
- `generate_rsa_keypair(bits=3072) -> (private_pem, public_pem)`
- `load_{signing,rsa}_{private,public}_key(pem)` — raise
  `KeyConfigurationError` on empty/garbage/wrong-type
- Cached env-backed loaders: `sender_signing_private_key()`,
  `sender_signing_public_key()`, `settlement_rsa_private_key()`,
  `settlement_rsa_public_key()`, plus `reset_key_cache()` for tests
- `canonical_json(dict) -> bytes` (sorted keys, no whitespace, UTF-8)
- `b64u_encode/b64u_decode` (unpadded base64url; decode raises `ValueError`)
- `build_aad(packet_id=, sender_id=) -> bytes`
- `build_signing_bytes(packet_id=, sender_id=, created_at=,
  encrypted_key=, nonce=, ciphertext=, aad=) -> bytes`
- `derive_idempotency_key(signing_bytes) -> "settle:<sha256hex>"`
- `encrypt_payload(plaintext, rsa_public, aad) -> EncryptedPayload`
  (NamedTuple: encrypted_key, nonce, ciphertext, aad)
- `decrypt_payload(encrypted_key=, nonce=, ciphertext=, aad=,
  recipient_private_key=) -> bytes`, raises `DecryptionError`
- `sign_bytes(signing_bytes, ed25519_private) -> bytes` (64 bytes)
- `verify_signature(signing_bytes, signature, ed25519_public) -> None`,
  raises `SignatureVerificationError`. Returns None on success by design so
  a caller cannot misread a falsy return as "verified".
- Exceptions: `CryptoError` base, `KeyConfigurationError`,
  `SignatureVerificationError`, `DecryptionError`
- Constants: `AES_KEY_BITS=256`, `AES_KEY_BYTES=32`, `GCM_NONCE_BYTES=12`,
  `RSA_KEY_BITS=3072`, `IDEMPOTENCY_KEY_PREFIX="settle:"`

`tests/conftest.py` provides session-scoped `keys` and `other_keys`
fixtures (`KeyBundle` with `.signing_private`, `.signing_public`,
`.rsa_private`, `.rsa_public`, and the matching `*_pem` strings).

## Assumptions
- **Python version**: RULES.md and ARCHITECTURE.md both specify Python
  3.12. Machine default is 3.13.3. Two 3.12 installs exist; the
  python.org one is 3.12.5, which `black` refuses to run on (known
  CPython 3.12.5 AST/memory-safety bug — black hard-errors and tells you
  to move off that patch). Using the Homebrew
  `/usr/local/opt/python@3.12/bin/python3.12` (3.12.9) for the venv
  instead, so we stay on the specified 3.12 line and still satisfy the
  RULES.md requirement to format with black.
- **Relay and bridge verify signatures, not just settlement**:
  ARCHITECTURE.md says "nothing is trusted or re-derived along the way
  except at the final settlement service", while RULES.md says "every
  endpoint that accepts a packet must independently verify the
  cryptographic signature before doing anything else with the payload".
  RULES.md wins (it is stated to override defaults), and the two reconcile
  cleanly: relay and bridge verify so forged packets are dropped early
  instead of consuming hops and queue space, and settlement still verifies
  independently and trusts nothing upstream. No node except settlement can
  decrypt, and no node's verdict is taken on faith by settlement.
- **`tests/integration/` added**: ARCHITECTURE.md lists only `unit/`,
  `concurrency/` and `load/` under `tests/`, but TASK.md Phase 3 requires
  an integration test and Phase 5 requires end-to-end compose
  verification. Those are neither unit nor concurrency nor load tests, so
  they get their own directory. Additive; nothing was moved.
- **Integration tests skip instead of failing when infrastructure is
  absent**: broker/database-backed tests are marked `integration` and skip
  when nothing is reachable, so `pytest` stays runnable without Docker
  while the real paths still get exercised when infrastructure is up. The
  real-broker path is genuinely covered, not just the in-memory fake.
- **`InMemoryPublisher` lives in `services/bridge/publisher.py`, not in
  tests**: it is used by tests, but keeping it beside the Protocol it
  implements means the two cannot drift, and it lets the demo flow run
  without a broker.
- **`shared/logging.py` added**: ARCHITECTURE.md lists only `crypto.py`,
  `models.py`, `config.py` under `shared/`, but RULES.md requires that
  every rejected packet is logged with a reason and that no secret is ever
  logged. That needs one shared logging setup rather than a copy per
  service, so I added `shared/logging.py`. Additive, nothing moved.
- **No `from __future__ import annotations` in service app modules**: the
  slowapi rate-limit decorator wraps the route function, and FastAPI
  resolves string annotations against the wrapper's module globals, where
  our model names don't exist. This surfaced as
  `PydanticUserError: ... is not fully defined` and the body param being
  misread as a query param. Using real annotation objects in service
  modules avoids it. `shared/` modules still use the future import.
- **Rate limiter is per-app, not module-level**: slowapi keys registered
  limits by endpoint name, so one shared `Limiter` plus a `create_app()`
  factory accumulates a duplicate limit on every factory call, and each
  request then consumes several hits from the same budget. Caught this
  because the 60/minute budget measured as 30. The limiter is now created
  inside `create_app()`.
- **`idempotency_key` is never transmitted**: it started as a Pydantic
  computed field, which serialized it into the packet and then failed
  re-parsing under `extra="forbid"`. Rather than allow it on the wire, it
  is now a plain property. Better posture anyway: every node derives the
  dedupe key from the bytes it actually received, so a sender cannot
  supply a key that disagrees with its own packet to dodge deduplication.
  `PacketCreatedResponse` exposes it as a convenience of that response
  envelope only.
- **Git remote**: no remote existed. The build prompt requires a push
  after every phase, so a remote is needed. Created the GitHub repo as
  **private** rather than public, since publishing is hard to reverse and
  visibility was never specified. Flip with
  `gh repo edit --visibility public` if public was intended.
- **Packet schema location**: Phase 2 in TASK.md says to define the
  Pydantic packet schema "matching ARCHITECTURE.md", but the supplied
  ARCHITECTURE.md content did not actually specify a packet format. I
  added a `## Packet Format` section to ARCHITECTURE.md defining the
  envelope, the canonical signing bytes, the AAD binding, and the
  idempotency key derivation, so Phase 2 has a concrete spec to match.
  This is additive, nothing in the supplied content was removed.
- **Signing algorithm**: ARCHITECTURE.md allows "RSA-PSS or Ed25519".
  Chose Ed25519 for signing (small fixed 64-byte signatures,
  no padding-parameter footguns, faster verification, which matters
  because the settlement consumer verifies on every packet including
  duplicates). RSA-OAEP is still used for AES key wrapping as specified,
  so both algorithms named in the stack are present and each is used
  where it fits.
- **`hops` excluded from the signed region**: relay nodes append a hop
  record when forwarding, so signing `hops` would invalidate the
  signature at the first hop. Hops are untrusted observability metadata
  and never feed a settlement decision. The "packet is identical through
  every hop" property in ARCHITECTURE.md therefore applies to the signed
  region (header + envelope + signature), which is what settlement
  actually verifies.
- **Money representation**: amounts are integer minor units
  (`amount_minor`, e.g. paise), never floats, to avoid rounding error in
  a system whose whole point is settlement correctness.

## Known Issues
(none yet)

## Real Measured Numbers (fill in only from actual test runs)
- Phase 1 crypto unit tests: 53 tests, 53 passed, 0 failed.
  Command: `.venv/bin/python -m pytest tests/unit/test_crypto.py -q`
  Run 2026-09-21, wall time 2.94s. Includes 9 tamper-rejection cases
  (ciphertext, each header field, each envelope byte field, signature bit
  flip, signature truncation, empty signature, foreign-signer forgery).
- Phase 2 unit suite (crypto + models + sender): 136 tests, 136 passed,
  0 failed. Command: `.venv/bin/python -m pytest tests/unit -q`
  Run 2026-09-21, wall time 4.64s. Confirmed stable in isolation and in
  full-suite order (two consecutive runs, same result).
- Phase 3 full suite: 182 tests, 182 passed, 0 failed.
  Command: `.venv/bin/python -m pytest -q`
  Run 2026-09-21, wall time 6.22s. Includes 40 relay/bridge/mesh-journey
  tests and 6 real-broker tests against RabbitMQ 3.13 in Docker.
- Phase 3 multi-hop integrity, measured: a packet was driven through
  1, 2, 3, 5 and 8 real serialize/HTTP/parse hops
  (`test_packet_survives_n_hops_unmodified`). At every hop count the
  signing bytes, signature, envelope and idempotency key were identical to
  the originals, and the packet still verified. Two copies of the same
  instruction sent via 1-hop and 4-hop paths produced the same idempotency
  key.
- Phase 3 broker behaviour, measured against real RabbitMQ: 5 duplicate
  publishes of one packet were all delivered (the broker does not dedupe,
  confirming dedupe must happen in settlement), messages came back with
  `delivery_mode=PERSISTENT` and `message_id` equal to the idempotency key,
  and bodies were byte-identical to what was published.
- Sender rate limit, measured not assumed: with
  `RATE_LIMIT_PER_MINUTE=60`, exactly the first 60 POST `/packets`
  requests returned 201 and request 61 onward returned 429
  (`test_packet_endpoint_is_rate_limited` asserts `first_limited == 60`).
- Concurrency test (Phase 4): not yet run
- Tamper test at settlement layer (Phase 4): not yet run
- Load test (Phase 6): not yet run
