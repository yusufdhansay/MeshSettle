# MeshSettle — Development Rulebook

## General Principles
- Correctness over speed of delivery. This project's entire value is
  that its concurrency guarantees actually hold. Do not skip or weaken
  a test to make a phase "done" faster.
- No invented metrics anywhere, not in code comments, not in README,
  not in commit messages. If a number isn't from an actual test run
  you executed, don't write it down.
- Every phase must leave the repo in a working, tested state before
  you move to the next phase.
- If you hit a genuine architectural ambiguity not resolved in PRD.md
  or ARCHITECTURE.md, write your decision and reasoning into
  `MEMORY.md` under `## Assumptions` and proceed. Don't block.

## Tech and Coding Standards
- Python 3.12, type hints on all function signatures
- Format with `black`, lint with `ruff`
- All services use FastAPI's async support properly, no blocking
  calls inside async routes
- All secrets (DB creds, RabbitMQ creds, RSA keys) come from
  environment variables, never hardcoded, never committed. Provide
  a `.env.example` with placeholder values.
- RSA/signing keys must persist across restarts (generate once, store
  as a mounted secret or env-injected PEM), not regenerated on every
  boot
- All Postgres access goes through SQLAlchemy with parameterized
  queries, no raw string-interpolated SQL, ever
- All external input (packet payloads) is validated with Pydantic
  before any processing
- Every endpoint that accepts a packet must independently verify the
  cryptographic signature before doing anything else with the payload
- Rate limit public-facing endpoints
- Idempotency check (Redis) must happen before database writes, not
  after, and must use an atomic operation (`SET NX`), never a
  read-then-write pattern
- Write a test for every piece of logic that touches money amounts,
  signatures, or deduplication before considering that logic done

## Project Structure
- Follow the folder structure in ARCHITECTURE.md exactly, don't
  reorganize it
- Each service has its own `Dockerfile`
- Shared code (crypto, models) lives in `shared/`, imported by each
  service, not duplicated
- Tests mirror the service structure: `tests/unit/test_<service>.py`
- Never commit `.env`, generated keys, or database data

## Error Handling
- Never fail silently. Log every rejected packet with a reason
  (bad signature, duplicate, malformed)
- Return specific error codes for: invalid signature, duplicate
  packet, malformed payload, internal error, so tests and load
  test reports can distinguish failure types
- If a phase's tests fail and you can't fix it within reasonable
  effort, log it clearly in `MEMORY.md` under `## Known Issues`,
  don't hide it or mark the phase complete anyway
