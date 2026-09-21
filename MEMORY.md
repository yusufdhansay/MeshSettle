# MeshSettle — Build Memory

This file is the persistent context across sessions. Read it first,
every time, before doing anything else.

## Current Phase
Phase 9: Minimal demo UI — next

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
  Commit `670545f` — 2026-09-21
- Phase 4 — Settlement service with exactly-once guarantee:
  `services/settlement/{db,dedupe,processor,consumer,app}.py`, Alembic
  setup (`alembic.ini`, `migrations/env.py`, one migration), and the
  correctness suites in `tests/concurrency/`. 255 tests passing.
  Found and fixed a real gap in the replay defence while writing the
  tamper test (see the AAD assumption below).
  Commit `a581939` — 2026-09-21
- Phase 5 — Dockerized services with compose orchestration: a Dockerfile
  per service, `docker-compose.yml`, `.dockerignore`,
  `scripts/bootstrap_env.py`, `scripts/verify_compose.sh`, and a new
  `POST /packets/submit` endpoint on the sender. Whole stack verified
  running: all four checks in the verification script passed. 263 tests
  passing.
  Commit `e0cb1fd` — 2026-09-21
- Phase 6 — Load test with captured baseline numbers:
  `tests/load/locustfile.py` (two user classes) and
  `scripts/run_load_test.sh` (raises the rate limit, runs Locust headless,
  drains the queue, reconciles against Postgres, restores the limit).
  Ran 50 users for 60s against the compose stack: 10,543 requests, zero
  failures, 176.57 req/s aggregate, p50 140ms, p95 250ms, and exactly-once
  held under load. Raw artifacts in `tests/load/results/`.
  Commit `c4777f8` — 2026-09-21
- Phase 7 — Kubernetes manifests for the settlement service: the four
  files ARCHITECTURE.md specifies plus a Secret template, a dev-only
  dependencies manifest, `scripts/k8s_secret.sh` and
  `scripts/verify_k8s.sh`. **Verified against a real cluster** (kind
  v0.33.0, Kubernetes v1.37.0), not just dry-run: all 10 checks passed,
  including exactly-once across 5 replicas.
  Commit `7c4d5ef` — 2026-09-21
- Phase 8 — CI pipeline and security hardening:
  `.github/workflows/ci.yml` (5 jobs) and `scripts/security_check.sh`
  (40 assertions). The security pass found **17 real CVEs** in 4 pinned
  dependencies, including `cryptography`; all were fixed by upgrading, and
  the audit is now clean. Suite, compose verification and images all
  re-verified on the upgraded stack.
  Commit `47221ab` — 2026-09-21

## In Progress
Nothing in flight. Phase 8 closed, Phase 9 (demo UI) not yet started.

## CI
`.github/workflows/ci.yml`, on every push and PR, with
`cancel-in-progress` concurrency:
- **lint** — `ruff check` and `black --check` over
  shared/services/tests/scripts/migrations
- **test** — the full suite against **real** Postgres 16, Redis 7 and
  RabbitMQ 3.13 service containers, not skipped. It also asserts each
  dependency is reachable *before* running, and afterwards fails the job if
  any `integration`-marked test reported a skip. Without that guard a green
  run could silently mean the exactly-once and tamper suites never
  executed, which would make CI actively misleading. Also runs
  `alembic upgrade head` → `downgrade base` → `upgrade head`.
- **security** — `pip-audit` plus `scripts/security_check.sh`, with
  `fetch-depth: 0` so the secret scan can inspect full history (a key
  committed and later deleted is still leaked)
- **build** — matrix over the four services, builds each image with GHA
  layer caching, and asserts the image does not run as uid 0
- **compose** — generates throwaway env files, validates
  `docker compose config`, and confirms `.env`/`.env.infra` are gitignored

`./scripts/security_check.sh` is the same gate CI runs, runnable locally.
40 assertions across 10 areas: no secret files tracked or committed in
history, no real private keys in tracked content, no hardcoded credentials,
no shell interpolation of credentials in compose, no DB URL in alembic.ini,
signature verification on every packet-accepting endpoint **and in the
right order** (verify before claim, claim before write, checked by line
number), atomic `SET NX` dedupe plus the unique constraint, rate limiting on
all four services with per-app limiters, input validation, no
string-interpolated SQL, log redaction, and non-root containers.

## Kubernetes
Files in `k8s/`:
- `configmap.yaml` — non-secret config only (hosts, ports, TTLs, rate
  limit, log level). Safe to commit.
- `secret.example.yaml` — template with placeholders, no real values.
  Documents which keys settlement gets and, importantly, which it must not:
  the sender's signing **private** key is never deployed, because
  settlement has no business being able to mint packets.
- `settlement-deployment.yaml` — Deployment (2 replicas to start,
  `maxUnavailable: 0` so a rollout never empties the consumer set) plus the
  Alembic migration Job. Non-root uid 1001, `readOnlyRootFilesystem: true`
  with an emptyDir at /tmp, all capabilities dropped,
  `automountServiceAccountToken: false`, liveness/readiness/startup probes,
  and CPU/memory requests (required, or the HPA has no denominator).
- `settlement-service.yaml` — ClusterIP. Not a LoadBalancer: the API is
  read-only and internal, and there is no HTTP write path at all.
- `settlement-hpa.yaml` — autoscaling/v2, 2..10 replicas, CPU 70% and
  memory 80%, scale up fast (30s window) and down slowly (300s window)
  because mesh traffic is bursty. A queue-depth External metric is included
  **commented out**, with the reason: an HPA referencing a metric no
  adapter serves reports `FailedGetExternalMetric` and then refuses to
  scale at all, which is worse than scaling on CPU.
- `dependencies.yaml` — Postgres/Redis/RabbitMQ for local cluster testing
  only, clearly labelled as such (single replica, emptyDir, no backups).
  Exists so the manifests can actually be verified.

Scripts:
- `./scripts/k8s_secret.sh [namespace]` creates the Secret from `.env`, so
  cluster keys match the ones the sender signs with. Nothing is written to
  disk and no value is echoed.
- `./scripts/verify_k8s.sh [--keep]` creates a throwaway kind cluster,
  installs metrics-server (patched with `--kubelet-insecure-tls`, which kind
  requires or the HPA reports `<unknown>` forever), loads the image, applies
  everything, and runs 10 checks. `--keep` leaves the cluster for debugging.
Requires a built `meshsettle/settlement:local` image (`docker compose build`)
and a `.env`.

## Load testing
`tests/load/locustfile.py` has two user classes, weighted 4:1:
- `PaymentSender` (weight 4) hits `POST /packets/submit`, so each task is a
  distinct payment going through signing, sealing, two relay hops, the
  bridge and the queue. This is the "how many payments can enter the
  system" number.
- `DuplicateFlooder` (weight 1) mints one packet in `on_start` and then
  replays that same packet at `POST /relay` for the rest of the run, which
  puts the exactly-once guarantee under sustained concurrent load.
A 429 is recorded as a failure on purpose, so a throughput number can never
be inflated by counting rate-limited requests as successes.

Run it with `./scripts/run_load_test.sh [USERS] [SPAWN_RATE] [DURATION]`
(defaults 50 / 10 / 60s). The script raises `RATE_LIMIT_PER_MINUTE` to
`LOAD_RATE_LIMIT` (default 1000000) for the duration, recreates sender,
mesh_relay **and bridge**, waits for the queue to drain after the run,
verifies no idempotency key or packet id settled twice, and restores the
original limit via an EXIT trap.

## Running the stack (Phase 5 onward)
```
python scripts/bootstrap_env.py     # once; writes .env and .env.infra
docker compose up --build -d
./scripts/verify_compose.sh         # asserts the whole flow end to end
docker compose down                 # add -v to wipe the volumes too
```
Host ports are offset so the stack coexists with anything already local:
Postgres 15432, Redis 16379, RabbitMQ 15672 (management UI 15673),
sender 18001, mesh_relay 18002, bridge 18003, settlement 18004.

Images: `meshsettle/{sender,mesh_relay,bridge,settlement}:local`. All are
two-stage builds on `python:3.12.9-slim-bookworm`, run as an unprivileged
`meshsettle` user (uid 1001), and healthcheck via the interpreter because
there is no curl in the image. The settlement image also carries
`migrations/` and `alembic.ini`, and compose reuses it as a one-shot
`migrate` service (`alembic upgrade head`) that the settlement service
waits on via `service_completed_successfully`. Using one image for both
means the migration applied always matches the running code.

`scripts/bootstrap_env.py` writes both env files from one source of truth
so credentials cannot drift (`--force` to regenerate; that invalidates
previously signed packets). `.env` holds everything including the PEMs;
`.env.infra` holds only the Postgres and RabbitMQ credentials, so the
infrastructure containers never receive private keys.

`scripts/verify_compose.sh` asserts, against the live stack: all services
healthy and consuming; one payment completes the full journey with the
exact sealed amount and 2 recorded hops; N concurrent copies produce
exactly 1 settlement row; corrupted packets are rejected by relay and
bridge; and settlement independently rejects corrupted packets published
straight to the queue. Exits non-zero on the first failed assertion.
Tunable via `DUPLICATES` and `CORRUPTED` env vars.

## Settlement API reference (for later phases)
`services/settlement/db.py`:
- `Base`, `Settlement`, `RejectedPacket` ORM models
- `settlements` columns: id, idempotency_key, packet_id, sender_id,
  payer_id, payee_id, amount_minor, currency, packet_created_at,
  settled_at, hop_count. Constraints:
  `uq_settlements_idempotency_key` (the durable exactly-once guarantee),
  `uq_settlements_packet_id`, `ck_settlements_amount_positive`,
  `ck_settlements_hop_count_non_negative`.
- `rejected_packets` columns: id, idempotency_key (nullable), packet_id
  (nullable), reason, detail, rejected_at
- `create_engine(dsn=None)`, `create_session_factory(engine)`,
  `create_all(engine)`, `drop_all(engine)` (tests only)
- `settlement_exists`, `get_settlement`, `count_settlements(session,
  key=None)`, `count_rejections(session, reason=None)`,
  `record_rejection(session, reason=, detail=, idempotency_key=,
  packet_id=)`

`services/settlement/dedupe.py`:
- `ClaimOutcome` StrEnum: `CLAIMED`, `IN_FLIGHT`, `ALREADY_SETTLED`
- `DedupeStore(client, claim_ttl_seconds=None, settled_ttl_seconds=None)`
  with `.claim(key)`, `.mark_settled(key)`, `.release_claim(key)`,
  `.state(key)`, `.ping()`
- `create_redis_client(url=None)`, `CLAIMED_MARKER`, `SETTLED_MARKER`
- Two marker lifetimes: `claimed` on a short TTL
  (`DEDUPE_CLAIM_TTL_SECONDS`, default 60) so a crashed consumer's claim
  expires and the broker's redelivery can win it; `settled` on a long TTL
  (`DEDUPE_TTL_SECONDS`, default 86400) so redelivered duplicates are
  rejected without touching Postgres.

`services/settlement/processor.py`:
- `SettlementProcessor(session_factory=, dedupe=, sender_public_key=,
  settlement_private_key=, metrics=None)` with `.process(raw_body)
  -> SettlementOutcome` and `.metrics`
- `SettlementOutcome(status, detail, code, idempotency_key, packet_id,
  settlement_id, retryable)` plus `.settled`
- `SettlementMetrics` with `.record(outcome)`, `.snapshot()`, `.rejected`
  and counters settled/duplicates/invalid_signature/malformed/
  decryption_failed/internal_errors
- Pipeline order: parse → verify signature → **AAD/header check** →
  Redis `SET NX` claim → decrypt → inner/outer packet_id cross-check →
  Postgres transaction → `mark_settled`. `IntegrityError` on insert is
  translated to `DUPLICATE_PACKET`.

`services/settlement/consumer.py`:
- `SettlementConsumer(processor, url=None, queue=None,
  prefetch_count=32, on_outcome=None)` with `.start()`, `.stop()`,
  `.wait_closed()`, `.queue_depth()`, `.queue_name`
- Acks settled and permanently-rejected messages; nacks with
  `requeue=True` only for retryable outcomes or an unexpected crash.
  Ack happens only after the Postgres commit.

`services/settlement/app.py`:
- `create_app()`, `app`; dependencies `get_session_factory`,
  `get_dedupe_store`, `get_metrics`, `get_consumer` (override in tests)
- Models `SettlementView`, `MetricsView`, `HealthView`
- Routes (all read-only): `GET /healthz`, `GET /metrics`,
  `GET /settlements/{idempotency_key}`, `GET /settlements?limit=`
  (clamped to 1..100). There is deliberately no HTTP write path.

## Migrations
`alembic.ini` sets `script_location = migrations` and deliberately does
NOT contain a database URL; `migrations/env.py` injects
`settings.postgres_sync_dsn` at runtime so credentials are never
committed. Migrations use the sync psycopg driver; the app uses asyncpg.
Current head: `9fe74eee6261` (create settlements and rejected_packets).
Verified `upgrade head` → `downgrade base` → `upgrade head` against real
Postgres 16.15. Run with the POSTGRES_* env vars pointing at your target.

## Test infrastructure for phases 4+
Containers used (ports chosen to avoid clashing with anything local):
- `docker run --rm --name meshsettle-pg-test -p 5433:5432 -e POSTGRES_USER=meshsettle -e POSTGRES_PASSWORD=testonly-localdev -e POSTGRES_DB=meshsettle postgres:16-alpine`
- `docker run --rm --name meshsettle-redis-test -p 6380:6379 redis:7-alpine`
- `docker run --rm --name meshsettle-rabbit-test --hostname meshrabbit -p 5673:5672 rabbitmq:3.13`
Env overrides honoured by the tests: `MESHSETTLE_TEST_PG_DSN`,
`MESHSETTLE_TEST_REDIS_URL`, `MESHSETTLE_TEST_AMQP_URL`. Every
infrastructure-backed test skips rather than fails when its dependency is
unreachable, so `pytest` works with no Docker at all.

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
- **The Phase 0 dependency pins were badly out of date and carried 17
  CVEs.** I pinned versions in Phase 0 from memory; it is now late 2026, so
  those pins were roughly two years stale. `pip-audit` caught it. Upgraded
  to `cryptography==50.0.1`, `fastapi==0.141.1`, `starlette==1.6.0`,
  `black==26.5.1`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`, re-pinned
  exactly, and re-verified everything. Lesson for later sessions: pinning is
  right, but the pins must come from a resolver run and an audit, not from
  recall.
- **Upgrading `cryptography` on this machine needed a Rust target and an
  x86_64 OpenSSL.** The host is arm64 but this venv's Python is an x86_64
  build running under Rosetta (`sysconfig.get_platform()` reports
  `macosx-14.0-x86_64`), and upstream no longer ships x86_64-macOS wheels
  past 48.0.1, so pip tried to compile. Fixed with
  `rustup target add x86_64-apple-darwin` and
  `OPENSSL_DIR=/usr/local/opt/openssl@3` (plus `OPENSSL_LIB_DIR`,
  `OPENSSL_INCLUDE_DIR`, `PKG_CONFIG_PATH`). This is a local-environment
  quirk only: CI (Linux x86_64) and the Docker images get manylinux wheels
  and need none of it. If a future session hits a cryptography build
  failure, that env-var set is the fix.
- **`pip-audit --strict` cannot be combined with `--skip-editable`**: strict
  treats the skipped editable install of this repo itself as an error, and
  without `--skip-editable` it errors because `meshsettle` is not on PyPI.
  CI uses `--skip-editable` alone; the exit code still fails on real
  findings, which is the behaviour that matters.
- **The CI Postgres password is a literal in the workflow on purpose.**
  GitHub Actions service containers need the credential on both sides, the
  container is created fresh per job and is only reachable from inside that
  job, so putting it in Actions secrets would be theatre. It is labelled in
  the workflow as CI-only and is used nowhere else.
- **`HTTP_422_UNPROCESSABLE_ENTITY` is deprecated** in the new Starlette;
  switched to `HTTP_422_UNPROCESSABLE_CONTENT`. The two remaining test
  warnings are third-party (httpx/starlette test client) and not ours.
- **`kind` was installed to make Phase 7 real** (`brew install kind`,
  v0.33.0). TASK.md allowed writing the manifests and noting that live
  verification was still needed, but kubectl alone cannot validate schemas
  without an API server, so "correct manifests" would have been an untested
  claim. A throwaway kind cluster turns it into a verified one.
- **`k8s/dependencies.yaml` and `k8s/secret.example.yaml` added** beyond the
  four files in the ARCHITECTURE.md tree. Without in-cluster Postgres, Redis
  and RabbitMQ the settlement manifests cannot be exercised at all, and
  without a Secret template there is nothing documenting which keys the
  service needs. `dependencies.yaml` is explicitly labelled test-only.
- **Postgres in Kubernetes needs `fsGroup: 70`**: the `postgres:16-alpine`
  image runs as uid/gid 70, and an emptyDir is mounted root-owned, so
  `initdb` cannot write and the pod never becomes ready. Diagnosed from the
  live cluster (pod stuck, no logs available). Setting `fsGroup` makes the
  kubelet chown the volume and set setgid.
- **`kind load docker-image` fails for Docker Desktop's multi-platform
  images** (`content digest ... not found`). The verify script tries
  `kind load`, falls back to `docker save` plus `kind load image-archive`,
  and if both fail just lets the node pull, with wait timeouts (600s) that
  cover a cold pull. An earlier run failed purely because a 180s wait was
  shorter than the pull time, which looked like a broken manifest but was
  not.
- **Internal mesh forwarding shares the relay's public rate-limit bucket**:
  slowapi keys limits by remote address, and the relay forwards hop 2 to
  itself, so with `MESH_HOP_COUNT=2` that internal call counts against the
  relay's own 60/min budget. Under load the relay started 429-ing its own
  forwarding, which the first hop correctly surfaced as a 502. Found during
  a 10-second smoke run before the real load test (17 `forward_failed`
  events, all `HTTPStatusError`). The load runner therefore raises the limit
  on sender, relay **and** bridge, not just the sender. See Known Issues:
  the real fix is to exempt or separately bucket peer-to-peer mesh traffic,
  which is a design change rather than a test-harness concern.
- **Load-test numbers are recorded with their conditions attached**, because
  a throughput figure without the rate limit, replica count and host is not
  a fact about the system. A 429 is counted as a Locust failure so the
  number cannot be quietly inflated by rate-limited requests.
- **No `${VAR}` interpolation in docker-compose.yml (this bit me for real)**:
  Compose resolves `${VAR}` from the invoking shell *before* falling back to
  `.env`. An `export POSTGRES_PASSWORD=...` left over from running Alembic
  earlier in the session meant Postgres initialized with the stale value
  while the services authenticated with the one from `.env`, producing
  `password authentication failed for user "meshsettle"` from the migrate
  container. Fixed by removing interpolation entirely: every service reads
  credentials only from an `env_file`. Note `$$` is still needed in the
  Postgres healthcheck so the variable expands inside the container.
- **Two env files, generated together**: `.env` (app config plus PEMs) and
  `.env.infra` (only Postgres/RabbitMQ credentials). The infra containers
  get the second one, so private keys never land in an environment that
  `docker inspect` exposes. `scripts/bootstrap_env.py` writes both at once
  so the passwords cannot drift, and chmods them 600.
- **`scripts/` directory added**: not in the ARCHITECTURE.md tree, but a
  fresh clone has no `.env` and therefore cannot start the stack, and the
  compose verification needs to be reproducible rather than a sequence of
  ad-hoc commands. Two scripts only: `bootstrap_env.py`, `verify_compose.sh`.
- **`POST /packets/submit` added to the sender**: TASK.md Phase 5 requires
  verifying "sender to settled" end to end and Phase 6 requires load
  testing "through the full pipeline", but the sender previously only
  created a packet and returned it, leaving the handoff to the caller. A
  real payer's device does both: seal the instruction, then pass it to a
  neighbour in range. `POST /packets` (create only) is kept, because the
  concurrency checks need to submit one identical packet repeatedly.
- **`tests/integration/results/` added** for compose verification
  transcripts, same rationale as `tests/concurrency/results/`.
- **`pyproject.toml` lists service subpackages explicitly**: `packages =
  ["shared", "services"]` did not include `services.sender` and friends, so
  `pip install .` inside the image produced a package that could not import
  its own service modules.
- **The AAD must be compared against the header explicitly (found by a
  test, and it was a real gap)**: I assumed AES-GCM's AAD authentication
  alone stopped an attacker lifting a sealed envelope onto a different
  header. It does not, because the attacker chooses which AAD to present:
  copy the original envelope *and* its original AAD onto a fresh header you
  sign yourself, and the GCM tag check passes. The replay test initially
  asserted `DECRYPTION_FAILED`, the code returned `MALFORMED_PAYLOAD` from
  the step 5 inner/outer packet_id cross-check, and that mismatch exposed
  it. The cross-check did catch the attack, but only incidentally and only
  *after* the replay had consumed an idempotency key. Added step 2a to the
  processor (and to ARCHITECTURE.md): reject when `envelope.aad !=
  expected_aad()`, before the dedupe claim. Lesson worth keeping: a
  cryptographic primitive only binds what you actually check.
- **Duplicates are logged but not persisted as rejections**: every other
  rejection reason gets a `rejected_packets` row, but duplicates only get a
  log line and a counter. Persisting them would mean a duplicate opens a
  Postgres transaction, which is exactly the thing the Redis layer exists to
  avoid, and would contradict the PRD's claim that duplicates never reach
  the database. `test_duplicate_is_rejected_before_postgres_is_touched`
  enforces this by swapping in a session factory that raises if used.
- **A rejected packet releases its claim; a settled one keeps it**: a
  permanently invalid packet will never settle under its key, so holding
  the key achieves nothing and `release_claim` keeps Redis clean. A settled
  packet's key is promoted to the long-lived `settled` marker.
- **`tests/concurrency/results/` added** to hold raw correctness output,
  mirroring `tests/load/results/`, because RULES.md requires every number
  in the repo to be traceable to an actual run.
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
- **Peer-to-peer mesh traffic is rate-limited as if it were public traffic.**
  The relay forwards hop 2 to itself, and slowapi buckets by remote address,
  so internal forwarding competes with external submissions for the same
  60/min budget. At production defaults this caps the mesh at roughly 30
  payments per minute per relay before the relay starts refusing its own
  forwards (surfacing as 502 at the entry hop). The load test works around it
  by raising the limit, which is fine for measurement but is not a fix.
  Proper fix: give peer relays a separate, higher bucket (or exempt them via
  an authenticated peer identity) so that public abuse protection and
  internal capacity are tuned independently. Not done yet because it changes
  the relay's trust model, which is beyond what Phase 6 called for.
- **Load figures are single-host and single-replica.** Client, all four
  services, Postgres, Redis and RabbitMQ shared one laptop CPU during the
  run, so the numbers are a floor for this configuration rather than a
  capacity estimate. Nothing in the repo claims otherwise.

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
- **Concurrency test (Phase 4), the headline number**: 50 duplicate packets
  fired simultaneously at the real processor, **1 settled**, 49 rejected as
  `DUPLICATE_PACKET`. Asserted in Postgres, not just in return values.
  Test: `test_fifty_simultaneous_duplicates_settle_exactly_once`.
- Exactly-once also verified at 2, 10, 100 and 250 concurrent duplicates:
  exactly 1 settlement row in every case
  (`test_exactly_once_holds_at_several_concurrency_levels`).
- Raw claim primitive: 100 concurrent `SET NX` claims on one key produced
  exactly 1 `CLAIMED` and 99 `IN_FLIGHT` (`test_only_one_claim_wins`).
- Redis-amnesia fallback: after settling a packet and flushing Redis
  entirely, a replay was still refused, caught by the Postgres unique
  constraint, leaving 1 settlement row. Also holds with 20 concurrent
  replays each preceded by a Redis flush.
- **Tamper test (Phase 4)**: 50 corrupted packets fired (8 distinct
  corruption types cycled: signature bit flip, sender_id swap, packet_id
  swap, created_at rewrite, ciphertext / encrypted_key / nonce / aad bit
  flips). **0 settled, 50 recorded as rejected** in the `rejected_packets`
  table. Test: `test_fifty_corrupted_packets_all_rejected_and_none_settle`.
- Full suite at end of Phase 4: **255 tests, 255 passed, 0 failed**,
  wall time 34.53s. Command `.venv/bin/python -m pytest -q`.
- Phase 4 concurrency/tamper raw output saved to
  `tests/concurrency/results/phase4-correctness-20260921T121301Z.txt`
  (49 tests, 49 passed, 20.96s), stamped with the real component versions
  it ran against: Python 3.12.9, PostgreSQL 16.15, Redis 7.4.11,
  RabbitMQ 3.13.7.
- **Phase 5 compose verification (real stack, all four checks passed)**,
  raw output in
  `tests/integration/results/phase5-compose-verification-20260921T140330Z.txt`,
  run 2026-09-21T14:03:33Z on Docker 29.7.2 / Compose 5.4.0:
  - all 4 services reported healthy, settlement reported `consuming: true`
  - one payment submitted via `POST /packets/submit` settled with
    `amount_minor` exactly 45678 and `hop_count` 2
  - 25 concurrent copies of one packet → exactly **1** settlement row
  - 10 corrupted packets → relay returned 400 for all 10; bridge returned
    400 `INVALID_SIGNATURE`
  - the same 10 corrupted packets published **directly to RabbitMQ**,
    bypassing relay and bridge → settlement's `invalid_signature` counter
    rose by exactly 10, `settled` did not change, and 20 rows accumulated
    in `rejected_packets`. This is the evidence that settlement verifies
    independently rather than trusting upstream nodes.
- Full suite at end of Phase 5: **263 tests, 263 passed, 0 failed**,
  wall time 38.57s.
- **Phase 6 load test (real run against the compose stack)**. Artifacts:
  `tests/load/results/phase6-load-20260921T141404Z-report.txt`,
  `..._stats.csv`, `..._stats_history.csv`, `..._stdout.txt`, `....html`.
  Run 2026-09-21T14:14:04Z. Locust 2.32.5, Python 3.12.9, Docker 29.7.2,
  Compose 5.4.0, host Darwin 25.6.0 x86_64.

  Conditions: 50 concurrent users, spawn rate 10/s, 60s run, a single
  replica of each service via docker compose, all on one laptop (so client,
  services and infrastructure share the same CPU). `RATE_LIMIT_PER_MINUTE`
  raised to 1,000,000 for the run; the production default is 60/min.

  Measured, from the saved `_stats.csv`:
  | endpoint | reqs | fails | req/s | p50 | p95 | p99 | max |
  |---|---|---|---|---|---|---|---|
  | `POST /packets/submit` (full pipeline) | 8,357 | 0 | 140.84 | 140ms | 250ms | 400ms | 763ms |
  | `POST /relay` (duplicate replay) | 2,149 | 0 | 36.22 | 130ms | 230ms | 380ms | 705ms |
  | aggregated | 10,516 | 0 | 177.23 | 140ms | 240ms | 390ms | 763ms |

  The end-of-run console summary reported 10,543 requests and 176.57 req/s;
  the CSV was flushed at a slightly different instant, hence the ~27 request
  difference. Both are in the saved artifacts. Zero failures either way.

  Correctness under load, which is the part that actually matters:
  - 8,412 new settlement rows, and the in-process `settled` counter also
    read 8,412, so the API's counters and the database agree exactly
  - **0 duplicate idempotency keys** and **0 packet ids settled twice**,
    asserted with `GROUP BY ... HAVING count(*) > 1` queries
  - 2,153 duplicates rejected during the run
  - queue depth was 0 immediately after the run, so settlement kept pace
    with ingestion rather than building a backlog
  - Locust recorded 8,357 pipeline submissions plus 10 minted packets
    (8,367) against 8,412 settlement rows. The excess is requests that the
    server completed but that were still in flight when Locust stopped
    counting at the run-time limit; with 50 users at a 140ms median, roughly
    50 requests are in flight at any instant, so an excess of ~45 is the
    expected order of magnitude rather than a lost-or-duplicated-payment
    signal. The uniqueness assertions above are what rule out duplication.

  Honest caveats on these numbers: single-replica, single-host, loopback
  networking, one relay container standing in for a multi-device mesh, and
  the rate limit lifted. They describe this configuration on this hardware
  and are not a claim about production capacity.
- **Phase 7 Kubernetes verification against a real cluster**, raw output in
  `tests/integration/results/phase7-k8s-verification-20260921T145458Z.txt`,
  run 2026-09-21T14:54:58Z. kind v0.33.0, node image kindest/node:v1.37.0,
  kubectl v1.36.1. All 10 checks passed, exit code 0:
  - every manifest parses, and ConfigMap/Deployment+Job/Service were
    accepted by a live API server via `--dry-run=server`
  - metrics-server became ready
  - the Alembic migration Job completed **in-cluster** (`1/1` in 6s)
  - the settlement Deployment rolled out with **2/2 replicas Ready**
  - the read API answered through the ClusterIP Service with
    `{"status":"ok","redis":true,"database":true,"consuming":true}`, so the
    pods reached Postgres, Redis and the broker
  - the HPA read **live** metrics rather than `<unknown>`:
    `cpu: 21%/70%, memory: 56%/80%`, `ScalingActive=True`
  - 3 copies of one signed packet published to the in-cluster queue across
    2 replicas → **exactly 1 settlement**, amount 31337 as sealed
  - **scaled to 5 replicas, published 200 messages (10 distinct packets ×
    20 copies) → exactly 10 settlements, 0 duplicate idempotency keys, 0
    packet ids settled twice.** This is the check that justifies the HPA:
    five independent pods, each with its own Redis connection and database
    session, racing on the same keys.
- **Phase 8 CI verified by an actual GitHub Actions run**, not just locally.
  Run `35619189056` on commit after `47221ab`, 2026-09-21T15:28:36Z: **all 8
  jobs green**. Lint 34s, Validate compose 23s, Tests 1m52s, Security 32s,
  and four image builds (27s to 50s). From the test job's own log:
  `postgres reachable / redis reachable / rabbitmq reachable`, then
  `263 passed, 2 warnings in 29.64s`, with
  `test_fifty_simultaneous_duplicates_settle_exactly_once PASSED` and
  `test_fifty_corrupted_packets_all_rejected_and_none_settle PASSED`
  explicitly present. So the exactly-once and tamper guarantees are enforced
  in CI against real Postgres/Redis/RabbitMQ, not skipped.
  The first attempt (run `35618819265`) failed one job: the compose
  validation job ran `bootstrap_env.py` without installing the package
  (`ModuleNotFoundError: No module named 'shared'`). Fixed by adding
  setup-python plus `pip install -e .` to that job.
- **Phase 8 security pass**, raw output in
  `tests/integration/results/phase8-security-20260921T152310Z.txt`, run
  2026-09-21T15:23:10Z:
  - `pip-audit --skip-editable`: **No known vulnerabilities found**, exit 0
  - `scripts/security_check.sh`: **40 PASS, 0 FAIL**, exit 0
  - Before the dependency upgrade the same audit reported **17 known
    vulnerabilities across 4 packages**: `cryptography` 44.0.0 (7
    advisories), `starlette` 0.41.3 (7), `black` 24.10.0 (2), `pytest`
    8.3.4 (1). Recorded here because it is the finding that mattered most in
    this phase, and because the fix is only meaningful next to the problem.
  - After upgrading, the full suite still passed (263/263, 35.68s), the
    images rebuilt, and `scripts/verify_compose.sh` passed every check
    against the upgraded stack (`cryptography 50.0.1, fastapi 0.141.1,
    starlette 1.6.0` confirmed inside the running container).
- An earlier load run at 14:12:00Z was discarded rather than kept: its
  baseline counts were taken after an earlier smoke test whose packets were
  still settling, so submissions could not be reconciled against settlements
  exactly. Its headline figures were consistent (10,349 requests, 0
  failures, 173.24 req/s, p50 140ms, p95 280ms). The retained run was done
  against a freshly wiped database (`docker compose down -v`) so the
  reconciliation is exact.
