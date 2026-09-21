# MeshSettle — Architecture

## High-Level Architecture

```
Sender Device --(signed+encrypted packet)--> Mesh Relay Node(s)
--(same packet, hop by hop)--> Bridge Node (has connectivity)
--(publish)--> RabbitMQ queue --(consume)--> Settlement Service
--(dedupe check)--> Redis --(if new)--> Postgres (settlement write)
```

Key property: the packet is identical from creation through every hop.
Nothing is trusted or re-derived along the way except at the final
settlement service, which independently verifies the signature and
checks for a duplicate hash before writing anything.

## Tech Stack

- Language: Python 3.12
- API/services: FastAPI
- Message queue: RabbitMQ (via `aio-pika` or `pika`)
- Deduplication store: Redis (atomic `SET key value NX` for idempotency)
- Database: PostgreSQL (via SQLAlchemy + Alembic for migrations)
- Crypto: `cryptography` library, RSA-OAEP for key exchange, AES-256-GCM
  for payload encryption, RSA-PSS or Ed25519 for signing
- Containers: Docker, `docker-compose` for local multi-service orchestration
- Orchestration: Kubernetes manifests (Deployment, Service, HPA), tested
  locally against `kind` or `minikube`
- Testing: `pytest`, `pytest-asyncio` for concurrency tests
- Load testing: Locust
- CI: GitHub Actions (lint, test, build image on every push)

## Folder Structure

```
meshsettle/
├── PRD.md
├── ARCHITECTURE.md
├── RULES.md
├── DESIGN.md
├── TASK.md
├── MEMORY.md
├── docker-compose.yml
├── .github/workflows/ci.yml
├── k8s/
│   ├── settlement-deployment.yaml
│   ├── settlement-service.yaml
│   ├── settlement-hpa.yaml
│   └── configmap.yaml
├── services/
│   ├── sender/            # creates and signs packets
│   ├── mesh_relay/        # simulates device-to-device hop
│   ├── bridge/             # publishes to RabbitMQ once "online"
│   └── settlement/         # consumes queue, dedupes, settles
├── shared/
│   ├── crypto.py           # signing, encryption, verification
│   ├── models.py           # shared Pydantic models
│   └── config.py
├── tests/
│   ├── unit/
│   ├── concurrency/         # exactly-once and tamper tests
│   └── load/                # Locustfile
├── migrations/               # Alembic
└── README.md
```

## How the parts connect

- `sender` and `mesh_relay` are lightweight FastAPI services with no
  persistent state, used to simulate the offline hop-by-hop journey
  in tests and demos
- `bridge` is the only component that touches RabbitMQ as a producer
- `settlement` is the only component that touches Redis and Postgres,
  and the only consumer of the queue
- Each service is independently containerized so the queue-based
  boundary between "offline simulation" and "settlement" is real,
  not just a function call
- Redis dedupe check happens before any Postgres write, so a duplicate
  packet never reaches the database or triggers a transaction at all

## Packet Format

The packet is the unit that travels unchanged from sender to settlement.
It is a JSON envelope with three parts: a cleartext routing header, an
encrypted payload, and a signature over the canonical bytes of both.

```
PaymentPacket
├── packet_id: UUID              # unique per transaction attempt
├── sender_id: str               # logical device/account identifier
├── created_at: datetime (UTC)   # ISO 8601, set once at creation
├── hops: list[HopRecord]        # append-only audit trail, NOT signed
├── envelope: EncryptedEnvelope
│   ├── encrypted_key: bytes     # AES-256 key, RSA-OAEP wrapped to settlement pubkey
│   ├── nonce: bytes             # 96-bit AES-GCM nonce, unique per packet
│   ├── ciphertext: bytes        # AES-256-GCM encrypted PaymentInstruction
│   └── aad: bytes               # additional authenticated data (see below)
└── signature: bytes             # Ed25519 signature over canonical signing bytes
```

`PaymentInstruction` (the plaintext inside `ciphertext`, never transmitted
in the clear):

```
PaymentInstruction
├── packet_id: UUID              # MUST equal envelope-level packet_id
├── payer_id: str
├── payee_id: str
├── amount_minor: int            # integer minor units (paise/cents), never float
├── currency: str                # ISO 4217, fixed "INR" for MVP
└── created_at: datetime (UTC)
```

### Canonical signing bytes

The signature covers a deterministic serialization so that any single-bit
change anywhere in the signed region invalidates it:

```
signing_bytes = canonical_json({
    packet_id, sender_id, created_at,
    encrypted_key, nonce, ciphertext, aad     # all base64url, no padding
})
```

`canonical_json` means sorted keys, no whitespace, UTF-8. `hops` is
deliberately excluded from the signed region: relay nodes append hop
records as they forward, so including hops would break the signature at
every hop. Hops are therefore untrusted metadata used only for
observability, never for settlement decisions.

### AAD binding

`aad` is the canonical JSON of `{packet_id, sender_id}`. Because AES-GCM
authenticates the AAD, a packet's ciphertext cannot be lifted out and
replayed under a different `packet_id` or `sender_id` without the GCM tag
check failing. This closes the gap where an attacker swaps the cleartext
header while keeping a validly signed ciphertext.

### Idempotency key

The dedupe key is derived from the signed region only:

```
idempotency_key = "settle:" + sha256(signing_bytes).hexdigest()
```

Two packets that differ only in `hops` produce the same key, which is
exactly the intent: the same instruction arriving via two different mesh
paths must settle once. A tampered packet produces a different key, but it
never reaches the dedupe step because signature verification runs first.

## Settlement Ordering Guarantee

The settlement service performs these steps in this exact order. The order
is the correctness property; reordering any two of these breaks a guarantee.

1. **Parse and validate** the packet with Pydantic. Malformed → reject
   (`MALFORMED_PAYLOAD`), never partially processed.
2. **Verify the Ed25519 signature** against the sender's registered public
   key, over the canonical signing bytes. Fail → reject
   (`INVALID_SIGNATURE`). Nothing below this line runs on unverified data.
3. **Atomic dedupe claim** in Redis: `SET idempotency_key <claim> NX EX
   <ttl>`. If the key already exists, the packet is a duplicate → reject
   (`DUPLICATE_PACKET`) and return before touching Postgres.
4. **Decrypt** the envelope (AES-256-GCM, key unwrapped via RSA-OAEP). GCM
   tag failure → reject (`DECRYPTION_FAILED`).
5. **Cross-check** that the inner `PaymentInstruction.packet_id` equals the
   envelope `packet_id` and `amount_minor > 0`. Mismatch → reject
   (`MALFORMED_PAYLOAD`).
6. **Write the settlement** to Postgres inside a single transaction.

### Why Redis before Postgres

Redis `SET NX` is a single atomic operation, so N concurrent consumers
racing on the same packet produce exactly one winner with no lock, no
retry loop, and no read-then-write window. Doing the dedupe check in
Postgres instead would still be correct via a unique constraint, but every
duplicate would open a transaction and hit the disk before being rejected.
Redis rejects duplicates before the database is involved at all.

### Why Postgres still has a unique constraint

Redis is the fast path, not the source of truth. The `settlements` table
carries a `UNIQUE` constraint on `idempotency_key` as a durable backstop,
so if Redis is flushed, evicts a key early, or a dedupe claim TTL expires
before settlement completes, the database still refuses the second write.
Belt and braces: Redis gives us cheap rejection, Postgres gives us the
guarantee that survives a cache loss.

### Crash-safety and the claim/commit split

The Redis claim is written *before* the Postgres commit, which opens a
window: if the consumer crashes between the two, the claim exists but no
settlement does, and a redelivery would be wrongly rejected as a
duplicate. To close this, the claim stores a state value (`claimed` vs
`settled`). A packet whose claim is still `claimed` with no matching
Postgres row is treated as a recoverable in-flight claim and allowed to
proceed, so the message is never silently lost. Queue messages are only
acked after the Postgres transaction commits.
