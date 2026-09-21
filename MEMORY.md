# MeshSettle — Build Memory

This file is the persistent context across sessions. Read it first,
every time, before doing anything else.

## Current Phase
Phase 1: Crypto core — next

## Completed Phases
- Phase 0 — Scaffolding: six root docs, folder tree per ARCHITECTURE.md,
  `pyproject.toml`, `.env.example`, `.gitignore`, `shared/config.py`,
  Python 3.12.9 venv with all deps installed. Lint + format clean.
  (commit hash recorded below at commit time) — 2026-09-21

## In Progress
Nothing in flight. Phase 0 closed, Phase 1 (crypto core) not yet started.

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
- Concurrency test: not yet run
- Tamper test: not yet run
- Load test: not yet run
