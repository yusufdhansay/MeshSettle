# MeshSettle — Build Memory

This file is the persistent context across sessions. Read it first,
every time, before doing anything else.

## Current Phase
Phase 2: Packet model and sender service — next

## Completed Phases
- Phase 0 — Scaffolding: six root docs, folder tree per ARCHITECTURE.md,
  `pyproject.toml`, `.env.example`, `.gitignore`, `shared/config.py`,
  Python 3.12.9 venv with all deps installed. Lint + format clean.
  Commit `d08f52a` — 2026-09-21
- Phase 1 — Crypto core: `shared/crypto.py` (Ed25519 sign/verify, RSA-OAEP
  key wrapping, AES-256-GCM payload encryption, canonical JSON, AAD
  binding, idempotency key derivation) and `shared/keygen.py`. 53 unit
  tests in `tests/unit/test_crypto.py`, all passing. Lint + format clean.
  Commit `<phase1>` — 2026-09-21

## In Progress
Nothing in flight. Phase 1 closed, Phase 2 (packet model + sender) not
yet started.

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
- Concurrency test (Phase 4): not yet run
- Tamper test at settlement layer (Phase 4): not yet run
- Load test (Phase 6): not yet run
