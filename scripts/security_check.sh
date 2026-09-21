#!/usr/bin/env bash
# Repository security gate.
#
# Checks the properties RULES.md and the PRD actually commit to, rather than
# printing a reassuring summary. Every check is an assertion that fails the
# build. Run it locally the same way CI does:
#
#   ./scripts/security_check.sh
#
# What it does NOT do: replace a real security review, or scan for every class
# of issue. It pins down the specific promises this repo makes.

set -uo pipefail

FAILURES=0

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; FAILURES=$((FAILURES + 1)); }
info() { printf '        %s\n' "$1"; }

section() {
  printf '\n[%s] %s\n' "$1" "$2"
}

echo "MeshSettle security gate"
echo "UTC: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=============================================================="

# --- 1. No secrets committed, ever --------------------------------------------
section 1 "no secret files are tracked"

for file in .env .env.infra k8s/secret.yaml .env.rate_limit_backup; do
  if git ls-files --error-unmatch "$file" >/dev/null 2>&1; then
    fail "$file is tracked in git"
  else
    pass "$file is not tracked"
  fi
done

for file in .env .env.infra k8s/secret.yaml; do
  if git check-ignore -q "$file"; then
    pass "$file is gitignored"
  else
    fail "$file is not gitignored"
  fi
done

# --- 2. No private keys in tracked content or history -------------------------
section 2 "no private key material in tracked files"

# secret.example.yaml legitimately contains the PEM header followed by
# REPLACE_ME, so a header match alone is not evidence of a leak. What matters is
# a header with real base64 key data after it.
LEAKS=0
while IFS= read -r tracked; do
  [ -f "$tracked" ] || continue
  case "$tracked" in
    *.png|*.jpg|*.gif|*.webp|*.html) continue ;;
  esac
  if grep -q -- "-----BEGIN .*PRIVATE KEY-----" "$tracked" 2>/dev/null; then
    # A real PKCS#8 body starts with MII... on the line after the header.
    if grep -A1 -- "-----BEGIN .*PRIVATE KEY-----" "$tracked" 2>/dev/null \
      | grep -qE "^\s*(MII|MC4)"; then
      fail "$tracked contains what looks like a real private key"
      LEAKS=$((LEAKS + 1))
    else
      info "$tracked has a PEM header with placeholder content (template, fine)"
    fi
  fi
done < <(git ls-files)
[ "$LEAKS" = "0" ] && pass "no real private keys in tracked files"

# Check history too: a key that was committed and later deleted is still leaked.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  HISTORY_HITS=$(git log --all -p --no-color 2>/dev/null \
    | grep -cE "^\+\s*(MII[A-Za-z0-9+/]{40,}|MC4[A-Za-z0-9+/]{20,})" || true)
  if [ "${HISTORY_HITS:-0}" -gt 0 ]; then
    fail "git history contains $HISTORY_HITS line(s) that look like private key bodies"
  else
    pass "no private key bodies anywhere in git history"
  fi
fi

# --- 3. Credentials come from the environment, not source ---------------------
section 3 "no hardcoded credentials in application code"

# Look for assignments of non-empty literal passwords in shipped code. The
# config module defines empty-string defaults, which is the correct pattern.
BAD_DEFAULTS=$(grep -rnE '(password|passwd|secret|token)\s*[:=]\s*"[^"]{6,}"' \
  shared/ services/ 2>/dev/null \
  | grep -viE '(REPLACE_ME|placeholder|example|\btest\b|redacted|marker)' || true)
if [ -n "$BAD_DEFAULTS" ]; then
  fail "possible hardcoded credential in application code:"
  echo "$BAD_DEFAULTS" | sed 's/^/        /'
else
  pass "no hardcoded credentials in shared/ or services/"
fi

# The compose file must not interpolate credentials from the invoking shell.
#
# `$$` is compose's escape: `$${POSTGRES_USER}` is passed through literally and
# expanded inside the container, which is fine and is what the Postgres
# healthcheck relies on. Only a single-`$` `${...}` is real interpolation. Strip
# the escaped form first so it is not mistaken for the dangerous one.
if sed 's/\$\$//g' docker-compose.yml 2>/dev/null \
  | grep -qE '\$\{[A-Z_]*(PASSWORD|USER|SECRET)[A-Z_]*\}'; then
  fail "docker-compose.yml interpolates credentials from the shell environment"
  info "shell values silently override .env, which has caused a real misconfiguration"
else
  pass "docker-compose.yml does not interpolate credentials from the shell"
fi

# Alembic must not carry a database URL in its committed config.
if grep -qE '^sqlalchemy\.url\s*=\s*\S' alembic.ini 2>/dev/null; then
  fail "alembic.ini contains a database URL"
else
  pass "alembic.ini has no committed database URL"
fi

# --- 4. Every packet-accepting endpoint verifies the signature ---------------
section 4 "signature verification on every packet-accepting endpoint"

for service in mesh_relay bridge; do
  if grep -q "packet.verify(" "services/$service/app.py" 2>/dev/null; then
    pass "$service verifies the packet signature"
  else
    fail "$service does not verify the packet signature"
  fi
done

if grep -q "packet.verify(self._sender_public_key)" services/settlement/processor.py 2>/dev/null; then
  pass "settlement verifies the packet signature independently"
else
  fail "settlement does not verify the packet signature"
fi

# The order matters: verification must precede the dedupe claim, or an attacker
# could burn idempotency keys for packets they cannot sign.
VERIFY_LINE=$(grep -n "packet.verify(self._sender_public_key)" \
  services/settlement/processor.py | head -1 | cut -d: -f1)
CLAIM_LINE=$(grep -n "self._dedupe.claim(" services/settlement/processor.py \
  | head -1 | cut -d: -f1)
if [ -n "$VERIFY_LINE" ] && [ -n "$CLAIM_LINE" ] && [ "$VERIFY_LINE" -lt "$CLAIM_LINE" ]; then
  pass "signature verification precedes the dedupe claim (line $VERIFY_LINE < $CLAIM_LINE)"
else
  fail "signature verification does not precede the dedupe claim"
fi

# And the dedupe claim must precede the database write.
WRITE_LINE=$(grep -n "_write_settlement(packet" services/settlement/processor.py \
  | head -1 | cut -d: -f1)
if [ -n "$CLAIM_LINE" ] && [ -n "$WRITE_LINE" ] && [ "$CLAIM_LINE" -lt "$WRITE_LINE" ]; then
  pass "dedupe claim precedes the settlement write (line $CLAIM_LINE < $WRITE_LINE)"
else
  fail "dedupe claim does not precede the settlement write"
fi

# --- 5. Deduplication is atomic ----------------------------------------------
section 5 "deduplication uses an atomic operation"

if grep -q "nx=True" services/settlement/dedupe.py 2>/dev/null; then
  pass "dedupe uses SET NX (atomic)"
else
  fail "dedupe does not use SET NX"
fi

if grep -qE "^\s*(current|existing)\s*=\s*await self\._client\.get" services/settlement/dedupe.py \
  && ! grep -q "nx=True" services/settlement/dedupe.py; then
  fail "dedupe looks like a read-then-write pattern"
else
  pass "dedupe is not a read-then-write pattern"
fi

# The durable backstop must exist in the schema.
if grep -q "uq_settlements_idempotency_key" services/settlement/db.py 2>/dev/null \
  && grep -q "uq_settlements_idempotency_key" migrations/versions/*.py 2>/dev/null; then
  pass "unique constraint on idempotency_key exists in the model and a migration"
else
  fail "unique constraint on idempotency_key is missing from the model or migrations"
fi

# --- 6. Rate limiting on public endpoints ------------------------------------
section 6 "rate limiting on public-facing endpoints"

for service in sender mesh_relay bridge settlement; do
  if grep -q "limiter.limit(" "services/$service/app.py" 2>/dev/null; then
    COUNT=$(grep -c "limiter.limit(" "services/$service/app.py")
    pass "$service rate limits $COUNT endpoint(s)"
  else
    fail "$service has no rate limiting"
  fi
done

# The limiter must be created per app, not at module scope, or repeated
# create_app() calls register duplicate limits and shrink the real budget.
for service in sender mesh_relay bridge settlement; do
  if grep -qE "^limiter\s*=\s*Limiter" "services/$service/app.py" 2>/dev/null; then
    fail "$service creates its Limiter at module scope"
  else
    pass "$service creates its Limiter inside create_app()"
  fi
done

# --- 7. Input validation ------------------------------------------------------
section 7 "external input is validated"

if grep -q 'extra="forbid"' shared/models.py; then
  COUNT=$(grep -c 'extra="forbid"' shared/models.py)
  pass "$COUNT model(s) reject unknown fields"
else
  fail "models do not reject unknown fields"
fi

if grep -q "MAX_AMOUNT_MINOR" shared/models.py && grep -q "gt=0" shared/models.py; then
  pass "amounts are bounded and must be positive"
else
  fail "amounts are not bounded"
fi

if grep -q "IDENTIFIER_PATTERN" shared/models.py; then
  pass "identifiers are pattern-constrained"
else
  fail "identifiers are not pattern-constrained"
fi

# --- 8. No raw SQL ------------------------------------------------------------
section 8 "no string-interpolated SQL"

RAW_SQL=$(grep -rnE '(execute|executemany)\s*\(\s*(f"|"[^"]*%s|.*\+\s*)' \
  shared/ services/ 2>/dev/null | grep -v "select(" || true)
if [ -n "$RAW_SQL" ]; then
  fail "possible string-interpolated SQL:"
  echo "$RAW_SQL" | sed 's/^/        /'
else
  pass "no string-interpolated SQL in shared/ or services/"
fi

# --- 9. Secrets are never logged ---------------------------------------------
section 9 "logging cannot leak secrets"

if grep -q "_REDACT_KEY_MARKERS" shared/logging.py \
  && grep -q "_redact_secrets" shared/logging.py; then
  pass "log processor redacts secret-looking fields"
else
  fail "no log redaction processor"
fi

if grep -q "def safe_fingerprint" shared/logging.py; then
  pass "safe_fingerprint exists for referring to key material without printing it"
else
  fail "no safe_fingerprint helper"
fi

# Nothing should log a raw signature or key.
LEAKY_LOGS=$(grep -rnE 'logger\.(info|warning|debug|error)\([^)]*(signature=packet\.signature|private_key=|=.*_pem)' \
  shared/ services/ 2>/dev/null || true)
if [ -n "$LEAKY_LOGS" ]; then
  fail "log call may emit key or signature material:"
  echo "$LEAKY_LOGS" | sed 's/^/        /'
else
  pass "no log call emits raw signatures or key material"
fi

# --- 10. Containers do not run as root ---------------------------------------
section 10 "containers run unprivileged"

for service in sender mesh_relay bridge settlement; do
  if grep -q "^USER meshsettle" "services/$service/Dockerfile"; then
    pass "$service Dockerfile drops to a non-root user"
  else
    fail "$service Dockerfile does not set a non-root USER"
  fi
done

if grep -q "runAsNonRoot: true" k8s/settlement-deployment.yaml \
  && grep -q "readOnlyRootFilesystem: true" k8s/settlement-deployment.yaml \
  && grep -q "drop:" k8s/settlement-deployment.yaml; then
  pass "k8s deployment sets runAsNonRoot, readOnlyRootFilesystem and drops capabilities"
else
  fail "k8s deployment is missing hardening settings"
fi

# --- Summary ------------------------------------------------------------------
echo
echo "=============================================================="
if [ "$FAILURES" -eq 0 ]; then
  echo "SECURITY GATE PASSED"
  exit 0
fi
echo "SECURITY GATE FAILED: $FAILURES check(s)" >&2
exit 1
