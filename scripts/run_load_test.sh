#!/usr/bin/env bash
# Run the Locust load test against the compose stack and capture real numbers.
#
# This script exists so the numbers in the README are reproducible and so the
# conditions they were measured under are recorded alongside them. It:
#
#   1. raises the sender/relay rate limit for the duration of the run, because
#      the production default (60/min) would turn a load test into a 429 test,
#      and records the value actually used
#   2. snapshots settlement and rejection counts before the run
#   3. runs Locust headless with CSV output
#   4. waits for the queue to drain, so settlement throughput is measured to
#      completion rather than to "handed to the broker"
#   5. reconciles what was submitted against what settled, and fails loudly if
#      the exactly-once guarantee did not hold under load
#   6. restores the original rate limit
#
# Usage:
#   docker compose up -d
#   ./scripts/run_load_test.sh [USERS] [SPAWN_RATE] [DURATION]
#
# Defaults: 50 users, spawn 10/s, 60s.

set -euo pipefail

USERS=${1:-50}
SPAWN_RATE=${2:-10}
DURATION=${3:-60s}
LOAD_RATE_LIMIT=${LOAD_RATE_LIMIT:-1000000}

PY=${PY:-.venv/bin/python}
LOCUST=${LOCUST:-.venv/bin/locust}
SETTLEMENT=http://127.0.0.1:18004

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS_DIR=tests/load/results
PREFIX="$RESULTS_DIR/phase6-load-$STAMP"

mkdir -p "$RESULTS_DIR"

psql_scalar() {
  docker compose exec -T postgres sh -c \
    "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"$1\"" | tr -d '[:space:]'
}

queue_depth() {
  curl -s "$SETTLEMENT/metrics" | "$PY" -c "
import json,sys
d = json.load(sys.stdin)
print(d['queue_depth'] if d['queue_depth'] is not None else -1)
"
}

metric() {
  curl -s "$SETTLEMENT/metrics" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['$1'])"
}

restore_rate_limit() {
  echo
  echo "restoring the original rate limit..."
  "$PY" - <<'PY'
import re
from pathlib import Path

path = Path(".env")
text = path.read_text()
original = Path(".env.rate_limit_backup").read_text().strip()
text = re.sub(r"^RATE_LIMIT_PER_MINUTE=.*$", f"RATE_LIMIT_PER_MINUTE={original}",
              text, flags=re.MULTILINE)
path.write_text(text)
Path(".env.rate_limit_backup").unlink()
print(f"  RATE_LIMIT_PER_MINUTE restored to {original}")
PY
  docker compose up -d --no-deps --force-recreate sender mesh_relay bridge >/dev/null 2>&1
  echo "  sender, mesh_relay and bridge restarted with the original limit"
}

echo "MeshSettle load test"
echo "UTC start   : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "users       : $USERS"
echo "spawn rate  : $SPAWN_RATE/s"
echo "duration    : $DURATION"
echo "=============================================================="

# --- Preflight ----------------------------------------------------------------
curl -sf --max-time 5 "$SETTLEMENT/healthz" >/dev/null \
  || { echo "settlement is not reachable; is the stack up?" >&2; exit 1; }
curl -s "$SETTLEMENT/healthz" | grep -q '"consuming":true' \
  || { echo "settlement is not consuming from the queue" >&2; exit 1; }

# --- 1. Raise the rate limit --------------------------------------------------
#
# All three HTTP services need it raised, not just the sender. The relay
# forwards the second hop to itself, and slowapi keys limits by remote address,
# so internal mesh traffic shares the relay's own public budget: leaving the
# limit at 60/min makes the relay 429 its own forwarding, which the first hop
# reports as a 502. The bridge receives every packet too.
echo
echo "[1] raising the rate limit for the duration of the run"
"$PY" - <<PY
import re
from pathlib import Path

path = Path(".env")
text = path.read_text()
match = re.search(r"^RATE_LIMIT_PER_MINUTE=(.*)$", text, flags=re.MULTILINE)
original = match.group(1) if match else "60"
Path(".env.rate_limit_backup").write_text(original)
text = re.sub(r"^RATE_LIMIT_PER_MINUTE=.*$",
              "RATE_LIMIT_PER_MINUTE=$LOAD_RATE_LIMIT", text, flags=re.MULTILINE)
path.write_text(text)
print(f"  was {original}, now $LOAD_RATE_LIMIT for this run")
PY
trap restore_rate_limit EXIT

docker compose up -d --no-deps --force-recreate sender mesh_relay bridge >/dev/null 2>&1
echo "  sender, mesh_relay and bridge recreated"
for _ in $(seq 1 40); do
  if curl -sf --max-time 3 http://127.0.0.1:18001/healthz >/dev/null 2>&1 \
    && curl -sf --max-time 3 http://127.0.0.1:18002/healthz >/dev/null 2>&1 \
    && curl -sf --max-time 3 http://127.0.0.1:18003/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
echo "  all three healthy again"

# --- 2. Baseline --------------------------------------------------------------
echo
echo "[2] baseline"
BEFORE_SETTLED=$(psql_scalar "select count(*) from settlements")
BEFORE_REJECTED=$(psql_scalar "select count(*) from rejected_packets")
echo "  settlement rows  : $BEFORE_SETTLED"
echo "  rejection rows   : $BEFORE_REJECTED"

# --- 3. Run Locust ------------------------------------------------------------
echo
echo "[3] running Locust headless"
set +e
"$LOCUST" \
  -f tests/load/locustfile.py \
  --headless \
  --users "$USERS" \
  --spawn-rate "$SPAWN_RATE" \
  --run-time "$DURATION" \
  --csv "$PREFIX" \
  --csv-full-history \
  --html "$PREFIX.html" \
  --only-summary \
  2>&1 | tee "$PREFIX-stdout.txt"
LOCUST_EXIT=${PIPESTATUS[0]}
set -e
echo "  locust exit code: $LOCUST_EXIT"

# --- 4. Let the queue drain ---------------------------------------------------
echo
echo "[4] waiting for the settlement queue to drain"
DRAIN_START=$(date +%s)
for _ in $(seq 1 240); do
  depth=$(queue_depth)
  [ "$depth" = "0" ] && break
  sleep 0.5
done
DRAIN_SECONDS=$(( $(date +%s) - DRAIN_START ))
echo "  queue depth now  : $(queue_depth)"
echo "  drain took       : ${DRAIN_SECONDS}s"

# --- 5. Reconcile -------------------------------------------------------------
echo
echo "[5] reconciling submissions against settlements"
AFTER_SETTLED=$(psql_scalar "select count(*) from settlements")
AFTER_REJECTED=$(psql_scalar "select count(*) from rejected_packets")
NEW_SETTLED=$((AFTER_SETTLED - BEFORE_SETTLED))
NEW_REJECTED=$((AFTER_REJECTED - BEFORE_REJECTED))

echo "  new settlements  : $NEW_SETTLED"
echo "  new rejections   : $NEW_REJECTED"
echo "  settled counter  : $(metric settled)"
echo "  duplicate counter: $(metric duplicates)"

# No settlement row may share an idempotency key. The unique constraint makes
# this impossible to violate, so this is a check that the constraint is real.
DUP_KEYS=$(psql_scalar "select count(*) from (select idempotency_key from settlements group by idempotency_key having count(*) > 1) d")
if [ "$DUP_KEYS" != "0" ]; then
  echo "  FAIL: $DUP_KEYS idempotency keys have more than one settlement row" >&2
  exit 1
fi
echo "  PASS: every settlement row has a unique idempotency key"

DUP_PACKETS=$(psql_scalar "select count(*) from (select packet_id from settlements group by packet_id having count(*) > 1) d")
if [ "$DUP_PACKETS" != "0" ]; then
  echo "  FAIL: $DUP_PACKETS packet ids settled more than once" >&2
  exit 1
fi
echo "  PASS: no packet id settled more than once"

# --- Report -------------------------------------------------------------------
{
  echo "MeshSettle Phase 6 load test"
  echo "UTC              : $STAMP"
  echo "Docker           : $(docker --version)"
  echo "Compose          : $(docker compose version --short)"
  echo "Python           : $($PY --version 2>&1)"
  echo "Locust           : $($LOCUST --version 2>&1 | head -1)"
  echo
  echo "Conditions"
  echo "  users                 : $USERS"
  echo "  spawn rate            : $SPAWN_RATE/s"
  echo "  run time              : $DURATION"
  echo "  rate limit during run : $LOAD_RATE_LIMIT/min (production default is 60/min)"
  echo "  topology              : single replica of each service via docker compose"
  echo "  host                  : $(uname -srm)"
  echo
  echo "Results"
  echo "  new settlement rows   : $NEW_SETTLED"
  echo "  new rejection rows    : $NEW_REJECTED"
  echo "  queue drain after run : ${DRAIN_SECONDS}s"
  echo "  duplicate idem keys   : $DUP_KEYS (must be 0)"
  echo "  packets settled twice : $DUP_PACKETS (must be 0)"
  echo
  echo "Locust summary (see the CSV files for full detail)"
  sed -n '/Type *Name/,$p' "$PREFIX-stdout.txt" | head -40
} > "$PREFIX-report.txt"

echo
echo "=============================================================="
echo "report : $PREFIX-report.txt"
echo "csv    : ${PREFIX}_stats.csv"
echo "html   : $PREFIX.html"
echo
echo "LOAD TEST COMPLETE"
