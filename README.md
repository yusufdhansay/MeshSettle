# MeshSettle

An offline-first payment settlement simulation. Two devices with no internet
connection exchange a signed, encrypted payment instruction over a local
transport. The instruction propagates device to device until one node regains
connectivity and forwards it to a backend, which settles it **exactly once**,
even when the same packet arrives many times through different paths.

The interesting problem is not the payment. It is settling correctly when
delivery is delayed, duplicated, or out of order.

> **Not affiliated with any real payment network.** MeshSettle is an original
> system. It is not UPI, not a UPI clone, and has no affiliation with,
> endorsement by, or connection to NPCI, UPI, or any bank or payment network.
> No real payment rails or network specifications are used or reimplemented
> here. What it borrows is only the general *problem statement* that UPI's
> offline mode and academic offline-payment research both address. **It moves
> no real money and is not safe to use for real money.**

---

## What this demonstrates

| Claim | How it is proven |
|---|---|
| Exactly-once settlement under concurrent duplicate delivery | 50 identical packets fired simultaneously at the real processor against real Redis and Postgres; exactly 1 settles |
| The guarantee survives losing the dedupe cache | Settle, then `FLUSHDB` Redis, then replay; the Postgres unique constraint refuses the second write |
| The guarantee survives horizontal scaling | 5 Kubernetes replicas, 200 messages (10 distinct packets × 20 copies); exactly 10 settlements |
| Tamper detection | 50 corrupted packets across 8 corruption types; 0 settle, all 50 recorded as rejected |
| A captured packet does not stay spendable forever | A validly signed packet dated 30 days ago is refused as `PACKET_EXPIRED`, before it can even consume an idempotency key |
| Settlement trusts nothing upstream | Corrupted packets published **directly to the queue**, bypassing relay and bridge, are still refused |
| It holds under load | 10,516 requests, 0 failures, 0 duplicate settlements |

Every number in this README comes from a run that is saved in the repository.
[Verified results](#verified-results) lists each one with its artifact and the
conditions it was measured under.

---

## Architecture

```
┌──────────┐   signed + encrypted packet    ┌──────────────┐
│  sender  │ ─────────────────────────────▶ │  mesh_relay  │ ─┐
│ (device) │                                │  (hop 1..N)  │  │ same packet,
└──────────┘                                └──────────────┘  │ hop by hop
                                                   ▲          │
                                                   └──────────┘
                                                        │ once the hop target
                                                        ▼ is reached
                                                 ┌──────────────┐
                                                 │    bridge    │
                                                 │ (has uplink) │
                                                 └──────┬───────┘
                                                        │ publish (durable,
                                                        ▼  persistent)
                                                 ┌──────────────┐
                                                 │   RabbitMQ   │
                                                 └──────┬───────┘
                                                        │ consume
                                                        ▼
                                                 ┌──────────────┐
                                                 │  settlement  │
                                                 └──┬────────┬──┘
                                        SET NX      │        │  if new
                                   ┌────────────────┘        └──────────┐
                                   ▼                                    ▼
                            ┌───────────┐                       ┌──────────────┐
                            │   Redis   │                       │   Postgres   │
                            │  (dedupe) │                       │ (settlement) │
                            └───────────┘                       └──────────────┘
```

The packet is byte-identical from creation through every hop. Nothing along the
way is trusted: the settlement service independently verifies the signature and
checks for a duplicate before writing anything.

### How exactly-once actually works

Five mechanisms, in this order. The order *is* the guarantee.

1. **Ed25519 signature check.** Nothing below this line runs on unauthenticated
   data. Verification precedes the dedupe claim deliberately: if claiming came
   first, anyone could submit a corrupted copy, burn the idempotency key, and
   make the real payment look like a duplicate.
2. **Freshness window** on the signed `created_at`: refuse anything older than
   24 hours (`PACKET_EXPIRED`) or dated more than 5 minutes ahead
   (`PACKET_NOT_YET_VALID`), both configurable. This is what stops a packet
   captured off the mesh from being spendable forever — exactly-once prevents a
   *second* settlement, but a captured packet that never settled once has
   nothing to collide with. Placed before the claim so a stale packet cannot
   burn the idempotency key belonging to the genuine packet.
3. **AAD/header consistency check.** The AES-GCM additional authenticated data
   must match the packet's own header. Without this, an attacker can lift a
   sealed envelope onto a fresh header they sign themselves and carry the
   original AAD along, and the GCM tag check passes.
4. **Atomic Redis claim** — `SET <key> claimed NX EX <ttl>`. `NX` makes it
   atomic, so N concurrent consumers produce exactly one winner with no lock, no
   retry loop, and no read-then-write window. A duplicate returns here and never
   opens a database transaction.
5. **Postgres unique constraint** on `idempotency_key`. Redis is the fast path,
   not the source of truth. This constraint is what makes the guarantee survive
   a cache flush, an evicted key, or an expired claim.

The dedupe key is `sha256` of the packet's *signed region*, so two copies that
travelled different mesh routes (and therefore carry different hop trails)
produce the same key and collapse to one settlement.

Full detail, including the crash-recovery reasoning behind the two Redis marker
lifetimes, is in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Quick start

Requires Docker and Python 3.12.

```bash
# 1. Generate local secrets (.env and .env.infra, both gitignored)
python scripts/bootstrap_env.py

# 2. Bring up everything: Postgres, Redis, RabbitMQ, migrations, 5 services
docker compose up --build -d

# 3. Prove it works, end to end, against the running stack
./scripts/verify_compose.sh
```

Then open **http://localhost:18000** for the demo UI. Send a payment, watch it
move through Created → Relayed → Bridged → Settled, then press
*"Send the same packet again"* and watch settlement refuse the duplicate while
the settled amount stays put.

| Service | URL | Purpose |
|---|---|---|
| ui | http://localhost:18000 | demo viewer |
| sender | http://localhost:18001 | creates signed, sealed packets |
| mesh_relay | http://localhost:18002 | simulates a device-to-device hop |
| bridge | http://localhost:18003 | publishes to the settlement queue |
| settlement | http://localhost:18004 | consumes, dedupes, settles (read-only HTTP) |

Host ports are offset so the stack coexists with anything already running
locally. Postgres is on 15432, Redis 16379, RabbitMQ 15672 (management UI
15673).

### Other things you can run

```bash
pytest                          # full suite; infra-backed tests skip without Docker
./scripts/security_check.sh     # the security gate CI runs
./scripts/run_load_test.sh      # Locust load test, writes to tests/load/results/
./scripts/verify_k8s.sh         # creates a kind cluster and verifies the manifests
```

---

## Verified results

Everything below was produced by a command in this repository, with the raw
output committed alongside it. Nothing here is estimated.

### Test suite

**322 tests, 322 passed**, 0 skipped, 48.60s (220 unit, 29 integration, 73
concurrency). Command: `pytest`. Also green in CI against real Postgres, Redis
and RabbitMQ service containers — see [CI](#ci) below.

### Exactly-once settlement

Artifact: [`tests/concurrency/results/phase4-correctness-20260921T121301Z.txt`](tests/concurrency/results/phase4-correctness-20260921T121301Z.txt)
(49 tests, 49 passed, 20.96s). Run against Python 3.12.9, PostgreSQL 16.15,
Redis 7.4.11, RabbitMQ 3.13.7.

| Scenario | Fired | Settled | Expected |
|---|---|---|---|
| Simultaneous duplicates of one packet | 50 | **1** | 1 |
| Same, at other concurrency levels | 2 / 10 / 100 / 250 | **1** each | 1 |
| Sequential redeliveries | 1 + 10 replays | **1** | 1 |
| Raw Redis `SET NX` race | 100 concurrent claims | **1 winner**, 99 in-flight | 1 |
| Replay after a full Redis `FLUSHDB` | 1 + 1 replay | **1** | 1 |
| 20 concurrent replays, each after a Redis flush | 20 | **1** | 1 |
| Distinct payments (dedupe must not over-collapse) | 25 distinct | **25** | 25 |

### Freshness window (replay defense)

Artifacts: [`tests/concurrency/results/phase11-freshness-20260921T164125Z.txt`](tests/concurrency/results/phase11-freshness-20260921T164125Z.txt)
(73 tests, 73 passed, 31.38s) and
[`tests/integration/results/phase11-freshness-live-20260921T164629Z.txt`](tests/integration/results/phase11-freshness-live-20260921T164629Z.txt).

| Packet age (window 3600s in the test) | Outcome |
|---|---|
| 3595s old (just inside) | **settles** |
| exactly at the edge | **settles** |
| 3605s old (just outside) | `PACKET_EXPIRED` |
| 1, 7, 30, 365 days old | `PACKET_EXPIRED` |
| 270s in the future (inside 300s skew) | **settles** |
| 2h, 1d, 30d in the future | `PACKET_NOT_YET_VALID` |

On the default 24-hour window: a 23h-old packet settles, a 25h-old one expires.

A stale packet is refused **before** the Redis claim, proven by swapping in a
dedupe store that raises if claimed — the same technique used to prove the
duplicate path never touches Postgres. Its idempotency key is confirmed absent
from Redis afterwards, so a stale packet cannot pre-emptively burn the key
belonging to the genuine packet with those bytes.

Verified live on the deployed stack: three validly signed packets published
**directly to RabbitMQ**, bypassing relay and bridge so settlement alone judged
them. The stale one and the future-dated one were refused (`expired: 1`,
`not_yet_valid: 1`) and the fresh one settled, readable back at
`id 10, amount_minor 31337`. Both rejections persisted with operator-legible
detail: `packet is 2592000s old, older than the 86400s freshness window`. The
`settled` counter in that transcript reads 5 rather than 1 because it is
cumulative over the container's lifetime, which already included earlier
verification packets.

**What this does not do:** an attacker who submits a captured packet *within*
the window still settles it once. The window removes the indefinite shelf life,
it does not remove the exposure. See [Known limitations](#known-limitations).

The 49 rejected duplicates in the headline case all reported
`DUPLICATE_PACKET`, and a test asserts the duplicate path never opens a Postgres
transaction by swapping in a session factory that raises if used.

### Tamper and forgery rejection

Same artifact as above.

| Scenario | Fired | Settled | Rejections recorded |
|---|---|---|---|
| Corrupted packets, 8 corruption types cycled | 50 | **0** | **50** |

The eight corruptions: signature bit flip, `sender_id` swap, `packet_id` swap,
`created_at` rewrite, and bit flips in `ciphertext`, `encrypted_key`, `nonce`
and `aad`. Also covered: packets signed by an unregistered key, an envelope
replayed under a new header, an inner/outer `packet_id` mismatch, and malformed
bodies. A separate test confirms a tampered copy cannot poison the genuine
packet's idempotency key.

### End to end through Docker Compose

Artifact: [`tests/integration/results/phase5-compose-verification-20260921T140330Z.txt`](tests/integration/results/phase5-compose-verification-20260921T140330Z.txt),
Docker 29.7.2 / Compose 5.4.0.

- All services healthy; settlement reported `consuming: true`
- One payment settled with `amount_minor` exactly **45678** and `hop_count` **2**
- **25** concurrent copies of one packet → exactly **1** settlement row
- **10** corrupted packets → relay returned 400 for all 10; bridge returned 400
  `INVALID_SIGNATURE`
- The same 10 published **directly to RabbitMQ**, bypassing relay and bridge →
  settlement's `invalid_signature` counter rose by exactly **10**, `settled` did
  not change. This is the evidence that settlement verifies independently rather
  than trusting upstream nodes.

### Load test

Artifacts: [`tests/load/results/phase6-load-20260921T141404Z-report.txt`](tests/load/results/phase6-load-20260921T141404Z-report.txt)
plus `_stats.csv`, `_stats_history.csv`, `-stdout.txt` and an HTML report.
Figures below are from the saved `_stats.csv`.

| Endpoint | Requests | Failures | req/s | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|
| `POST /packets/submit` (full pipeline) | 8,357 | **0** | 140.84 | 140ms | 250ms | 400ms | 763ms |
| `POST /relay` (duplicate replay) | 2,149 | **0** | 36.22 | 130ms | 230ms | 380ms | 705ms |
| Aggregated | 10,516 | **0** | 177.23 | 140ms | 240ms | 390ms | 763ms |

Correctness under that load:

- **8,412** settlement rows, and the service's own `settled` counter also read
  **8,412** — the API and the database agreed exactly
- **0** duplicate idempotency keys and **0** packet ids settled twice, asserted
  with `GROUP BY ... HAVING count(*) > 1`
- 2,153 duplicates refused
- Queue depth was **0** immediately after the run, so settlement kept pace with
  ingestion rather than building a backlog

**Conditions, because a throughput number without them is meaningless:** 50
concurrent users, spawn rate 10/s, 60s run, a single replica of each service via
Docker Compose, everything (load generator, all services, Postgres, Redis,
RabbitMQ) on one laptop sharing a CPU, `RATE_LIMIT_PER_MINUTE` raised to
1,000,000 for the run against a production default of 60, host Darwin 25.6.0
x86_64. `POST /packets/submit` does real work per request: Ed25519 signing,
AES-256-GCM sealing, RSA-OAEP key wrapping, two mesh hops, and a durable publish.

These figures describe this configuration on this hardware. They are a floor for
a single replica, not a capacity estimate.

The end-of-run console summary reported 10,543 requests and 176.57 req/s; the CSV
was flushed at a marginally different instant, hence the ~27 request difference.
Both are in the saved artifacts.

### Kubernetes

Artifact: [`tests/integration/results/phase7-k8s-verification-20260921T145458Z.txt`](tests/integration/results/phase7-k8s-verification-20260921T145458Z.txt),
exit code 0. Verified on a real cluster (kind v0.33.0, Kubernetes v1.37.0,
kubectl v1.36.1), not a dry run. All 10 checks passed:

- Every manifest accepted by a live API server via `kubectl apply --dry-run=server`
- The Alembic migration Job completed **in-cluster** (`1/1`, 6s)
- The Deployment rolled out with **2/2 replicas Ready**
- The read API answered through the ClusterIP Service with
  `{"status":"ok","redis":true,"database":true,"consuming":true}`
- The HPA reported **live** metrics and `ScalingActive=True`. At the moment of
  the check it read `memory: 63%/80%` with `cpu: <unknown>/70%`, CPU metrics
  not having landed yet that early after rollout; by the end of the run it read
  `cpu: 8%/70%, memory: 63%/80%`. The point being verified is that the
  autoscaler is wired to a working metrics pipeline rather than sitting at
  `<unknown>` indefinitely, which is what happens on kind without the
  `--kubelet-insecure-tls` patch the verification script applies.
- 3 copies of one packet across 2 replicas → exactly **1** settlement, amount
  31337 as sealed
- **Scaled to 5 replicas**, published 200 messages (10 distinct packets × 20
  copies) → exactly **10** settlements, **0** duplicate idempotency keys, **0**
  packet ids settled twice

That last check is why the HPA is safe to have: exactly-once does not depend on
there being a single consumer.

### Security

Artifact: [`tests/integration/results/phase8-security-20260921T152310Z.txt`](tests/integration/results/phase8-security-20260921T152310Z.txt).

- `pip-audit`: **No known vulnerabilities found**, exit 0
- `./scripts/security_check.sh`: **43 assertions, 0 failures**, exit 0

The audit earned its place: on first run it reported **17 known vulnerabilities
across 4 packages** (`cryptography` 44.0.0 with 7 advisories, `starlette`
0.41.3 with 7, `black` with 2, `pytest` with 1). All were fixed by upgrading,
and the suite, the images and the compose verification were re-run afterwards.

The gate is not a summary printer; it asserts things like *verification happens
before the dedupe claim, and the claim before the database write*, checked by
source line number.

### Demo UI

Artifact: [`tests/integration/results/phase9-ui-verification-20260921T154436Z.txt`](tests/integration/results/phase9-ui-verification-20260921T154436Z.txt).

A payment sent through the UI settled with `amount_minor` 45678 and `hop_count`
2. **3 replays of the identical packet** were all accepted by the mesh (202) and
all refused by settlement: the settlement row stayed at `id 6, amount_minor
45678`, with exactly **1** row in Postgres for that key.

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs 9 jobs on every push
and pull request: lint, tests, security, compose validation, and an image build
per service. Verified green on GitHub Actions.

The test job provisions **real** Postgres, Redis and RabbitMQ service
containers, asserts each is reachable before running, exercises
`alembic upgrade head → downgrade base → upgrade head`, and then fails the build
if any `integration`-marked test *skipped*. Without that last guard a green run
could silently mean the exactly-once and tamper suites never executed, which
would make CI worse than useless.

---

## Repository layout

```
services/
  sender/        creates and signs packets (the payer's device)
  mesh_relay/    simulates a device-to-device hop; carries, never reads
  bridge/        the node with connectivity; only RabbitMQ producer
  settlement/    consumes, dedupes, settles; only Redis and Postgres client
  ui/            demo viewer; serves the page and proxies four calls
shared/
  crypto.py      every cryptographic decision lives here
  models.py      the wire contract, shared by all services
  config.py      environment-driven configuration
  logging.py     structured logging with secret redaction
tests/
  unit/          220 tests
  integration/   29 tests, plus verification transcripts in results/
  concurrency/   49 tests: exactly-once and tamper, plus results/
  load/          Locust file, plus captured reports in results/
k8s/             settlement Deployment, Service, HPA, ConfigMap, Secret template
migrations/      Alembic
scripts/         bootstrap, verification, load test, security gate
```

## Documentation

| File | What it covers |
|---|---|
| [PRD.md](PRD.md) | goals, scope, and what is explicitly out of scope |
| [ARCHITECTURE.md](ARCHITECTURE.md) | packet format, signing bytes, and the settlement ordering guarantee |
| [RULES.md](RULES.md) | the development rulebook this was built under |
| [DESIGN.md](DESIGN.md) | the UI's visual language |
| [TASK.md](TASK.md) | the phase-by-phase plan |
| [MEMORY.md](MEMORY.md) | build log: decisions, assumptions, and every measured number |

## Known limitations

Stated plainly, because a portfolio project that hides its rough edges is
less useful than one that names them.

- **The freshness window bounds replay, it does not eliminate it.** A packet
  captured off the mesh and submitted *within* the 24-hour window still settles,
  once. What the window removes is indefinite shelf life. It also does nothing
  against an attacker replaying promptly with a synchronised clock — only
  against long-delayed replay. Closing the rest needs an online freshness
  challenge, which contradicts the offline premise. Shortening the window
  narrows exposure but costs offline tolerance, since a genuine payer offline
  for longer gets refused.
- **Freshness is judged against the settlement host's clock.** There is no
  trusted time source, so a badly wrong clock moves the window with it. Run NTP.
- **Peer-to-peer mesh traffic is rate-limited as if it were public traffic.**
  The relay forwards its second hop to itself, and the limiter buckets by remote
  address, so internal forwarding competes with external submissions for the
  same budget. At the default 60/min this caps a relay at roughly 30 payments
  per minute before it starts refusing its own forwards. The load test raises
  the limit to measure past it, which is fine for measurement but is not a fix.
  The real fix is a separate bucket for authenticated peer relays, which changes
  the relay's trust model.
- **Load figures are single-host and single-replica.** See the conditions above.
- **The mesh transport is HTTP, not Bluetooth or NFC.** Deliberate, and stated
  in the PRD. What is faithfully simulated is the store-and-forward, delayed,
  duplicate-prone delivery model, not the radio layer.
- **One relay container stands in for a multi-device mesh**, looping back to
  itself to accumulate hops.
- **The Kubernetes manifests cover the settlement service only.** The
  `dependencies.yaml` that provisions Postgres, Redis and RabbitMQ is labelled
  test-only: single replica, `emptyDir` volumes, no backups. Real deployments
  use managed services.
- **`k8s/secret.yaml` is a plaintext Secret.** Kubernetes Secrets are base64,
  not encrypted. Production should use Sealed Secrets, the External Secrets
  Operator, SOPS, or a cloud secret store.

## License and intent

A portfolio and reference system for demonstrating distributed systems
technique. Not a production payment application. It moves no real money.
