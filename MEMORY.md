# MeshSettle — Build Memory

This file is the persistent context across sessions. Read it first,
every time, before doing anything else.

## Current Phase
Phase 3: Mesh relay simulation and bridge — next

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
  Commit `<phase2>` — 2026-09-21

## In Progress
Nothing in flight. Phase 2 closed, Phase 3 (mesh relay + bridge) not yet
started.

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
- Sender rate limit, measured not assumed: with
  `RATE_LIMIT_PER_MINUTE=60`, exactly the first 60 POST `/packets`
  requests returned 201 and request 61 onward returned 429
  (`test_packet_endpoint_is_rate_limited` asserts `first_limited == 60`).
- Concurrency test (Phase 4): not yet run
- Tamper test at settlement layer (Phase 4): not yet run
- Load test (Phase 6): not yet run
