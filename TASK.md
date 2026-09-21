# MeshSettle — Task Breakdown

Work through these phases in order. Complete, test, update MEMORY.md,
commit and push after each one before starting the next.

## Phase 0: Scaffolding
- Create the six root docs (PRD, ARCHITECTURE, RULES, DESIGN, TASK, MEMORY)
- Create folder structure per ARCHITECTURE.md
- Set up `pyproject.toml` or `requirements.txt`, `.env.example`,
  `.gitignore`
- Initialize git repo if not already done, first commit

## Phase 1: Crypto core
- Implement `shared/crypto.py`: key generation (persisted, not
  regenerated per run), RSA-OAEP encryption/decryption, AES-256-GCM
  payload encryption, signing and signature verification
- Unit tests: valid packet verifies correctly, tampered payload is
  rejected, tampered signature is rejected
- Commit: "Phase 1: crypto core with signing and encryption"

## Phase 2: Packet model and sender service
- Define the packet schema (Pydantic) matching ARCHITECTURE.md
- Build `sender` service: creates a signed, encrypted packet from a
  simulated transaction request
- Unit tests for packet creation and structure
- Commit: "Phase 2: sender service and packet model"

## Phase 3: Mesh relay simulation and bridge
- Build `mesh_relay`: forwards a packet unchanged (simulating a hop),
  configurable number of hops
- Build `bridge`: receives a packet after N hops, publishes it to
  RabbitMQ
- Integration test: packet survives N simulated hops unmodified
- Commit: "Phase 3: mesh relay and bridge to queue"

## Phase 4: Settlement service (the core of the project)
- Build `settlement` service: consumes from RabbitMQ, verifies
  signature, checks Redis for duplicate via atomic `SET NX`, if new,
  writes settlement to Postgres inside a transaction
- Concurrency test: fire N (start with 50) duplicate/simultaneous
  packets at the consumer, assert exactly 1 settles in Postgres
- Tamper test: fire N corrupted packets, assert 0 settle and all are
  logged as rejected
- Commit: "Phase 4: settlement service with exactly-once guarantee"

## Phase 5: Dockerize and compose
- Dockerfile per service
- `docker-compose.yml` wiring RabbitMQ, Redis, Postgres, and all
  services together
- Verify the full flow (sender to settled) works end to end via
  docker-compose
- Commit: "Phase 5: dockerized services with compose orchestration"

## Phase 6: Load testing
- Write Locust load test simulating concurrent packet creation and
  submission through the full pipeline
- Run it, capture real throughput (req/sec) and latency (p50/p95)
  numbers, save the raw report into `tests/load/results/`
- Commit: "Phase 6: load test with captured baseline numbers"

## Phase 7: Kubernetes manifests
- Write Deployment, Service, and HPA manifests for the settlement
  service (scale based on CPU or a custom queue-depth metric if
  feasible)
- Test locally against `kind` or `minikube` if available; if not
  available in this environment, write the manifests correctly and
  note in MEMORY.md that live cluster verification is still needed
- Commit: "Phase 7: Kubernetes manifests for settlement service"

## Phase 8: CI and security pass
- GitHub Actions workflow: lint, run full test suite, build Docker
  images on every push
- Security pass: rate limiting on public endpoints, dependency
  vulnerability scan (`pip-audit` or similar), confirm no secrets in
  repo, confirm all input is validated
- Commit: "Phase 8: CI pipeline and security hardening"

## Phase 9: Minimal demo UI
- Build the minimal UI per DESIGN.md showing packet journey and status
- Commit: "Phase 9: demo UI"

## Phase 10: Final README and number consolidation
- Write a top-level README summarizing what the project does, the
  architecture diagram, and every real measured number from Phase 4
  and Phase 6, clearly labeled with how each was produced
- Commit: "Phase 10: final README with verified metrics"
