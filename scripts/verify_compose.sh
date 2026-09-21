#!/usr/bin/env bash
# End-to-end verification of the docker-compose stack.
#
# Proves four things against the running stack, not against mocks:
#   1. a payment travels sender -> relay -> bridge -> queue -> settled
#   2. N concurrent copies of one packet settle exactly once
#   3. corrupted packets are rejected at the mesh edge
#   4. settlement rejects corrupted packets independently, even when they are
#      published straight to the queue and never pass the relay or bridge
#
# Usage:
#   python scripts/bootstrap_env.py      # once, if .env does not exist
#   docker compose up --build -d
#   ./scripts/verify_compose.sh
#
# Exits non-zero on the first failed assertion.

set -euo pipefail

SENDER=http://127.0.0.1:18001
RELAY=http://127.0.0.1:18002
BRIDGE=http://127.0.0.1:18003
SETTLEMENT=http://127.0.0.1:18004

PY=${PY:-.venv/bin/python}
DUPLICATES=${DUPLICATES:-25}
CORRUPTED=${CORRUPTED:-10}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; exit 1; }

psql_scalar() {
  docker compose exec -T postgres sh -c \
    "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"$1\"" | tr -d '[:space:]'
}

metric() {
  curl -s "$SETTLEMENT/metrics" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['$1'])"
}

echo "MeshSettle compose verification"
echo "UTC: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "=============================================================="

# --- 0. Everything is up ------------------------------------------------------
echo
echo "[0] service health"
for entry in "sender $SENDER" "mesh_relay $RELAY" "bridge $BRIDGE" "settlement $SETTLEMENT"; do
  name=${entry%% *}; url=${entry##* }
  body=$(curl -s --max-time 5 "$url/healthz" || true)
  echo "$body" | grep -q '"status":"ok"' || fail "$name is not healthy: $body"
  pass "$name healthy"
done
echo "$(curl -s $SETTLEMENT/healthz)" | grep -q '"consuming":true' \
  || fail "settlement is not consuming from the queue"
pass "settlement is consuming from the queue"

BASELINE_SETTLED=$(psql_scalar "select count(*) from settlements")
echo "  baseline settlement rows: $BASELINE_SETTLED"

# --- 1. One payment, full journey --------------------------------------------
echo
echo "[1] a single payment travels the full pipeline"
curl -s --max-time 15 -X POST "$SENDER/packets/submit" \
  -H 'Content-Type: application/json' \
  -d '{"payer_id":"device-alice","payee_id":"device-bob","amount_minor":45678}' \
  -o "$WORK/submit.json"

KEY=$("$PY" -c "import json;print(json.load(open('$WORK/submit.json'))['idempotency_key'])")
echo "  idempotency key: $KEY"

settled=""
for _ in $(seq 1 60); do
  body=$(curl -s --max-time 5 "$SETTLEMENT/settlements/$KEY")
  if echo "$body" | grep -q '"amount_minor"'; then settled=$body; break; fi
  sleep 0.25
done
[ -n "$settled" ] || fail "payment did not settle within 15s"
echo "$settled" | grep -q '"amount_minor": *45678' || fail "settled amount is wrong: $settled"
pass "settled with the exact amount that was sealed"
echo "$settled" | grep -q '"hop_count": *2' || fail "expected 2 mesh hops: $settled"
pass "recorded 2 mesh hops"

# --- 2. Exactly-once under concurrency ---------------------------------------
echo
echo "[2] $DUPLICATES concurrent copies of one packet settle exactly once"
curl -s --max-time 10 -X POST "$SENDER/packets" -H 'Content-Type: application/json' \
  -d '{"payer_id":"device-dave","payee_id":"device-erin","amount_minor":99999}' \
  -o "$WORK/created.json"
"$PY" -c "
import json
d = json.load(open('$WORK/created.json'))
json.dump(d['packet'], open('$WORK/packet.json','w'))
open('$WORK/key','w').write(d['idempotency_key'])
"
DUP_KEY=$(cat "$WORK/key")

for _ in $(seq 1 "$DUPLICATES"); do
  curl -s --max-time 30 -X POST "$RELAY/relay" -H 'Content-Type: application/json' \
    --data-binary @"$WORK/packet.json" -o /dev/null &
done
wait
sleep 3

rows=$(psql_scalar "select count(*) from settlements where idempotency_key='$DUP_KEY'")
[ "$rows" = "1" ] || fail "expected exactly 1 settlement for the duplicated packet, got $rows"
pass "exactly 1 settlement row for $DUPLICATES concurrent copies"

# --- 3. Corrupted packets rejected at the mesh edge --------------------------
echo
echo "[3] $CORRUPTED corrupted packets are rejected by relay and bridge"
"$PY" - <<PY
import base64, json
pkt = json.load(open("$WORK/packet.json"))
dec = lambda s: base64.urlsafe_b64decode(s + "=" * ((-len(s)) % 4))
enc = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
raw = bytearray(dec(pkt["envelope"]["ciphertext"]))
for i in range($CORRUPTED):
    bad = bytearray(raw)
    bad[-1] ^= 1 << (i % 8)
    copy = json.loads(json.dumps(pkt))
    copy["envelope"]["ciphertext"] = enc(bytes(bad))
    json.dump(copy, open(f"$WORK/bad_{i}.json", "w"))
PY

for i in $(seq 0 $((CORRUPTED - 1))); do
  code=$(curl -s --max-time 10 -o /dev/null -w '%{http_code}' -X POST "$RELAY/relay" \
    -H 'Content-Type: application/json' --data-binary @"$WORK/bad_$i.json")
  [ "$code" = "400" ] || fail "relay accepted a corrupted packet (http $code)"
done
pass "relay rejected all $CORRUPTED corrupted packets with 400"

code=$(curl -s --max-time 10 -o "$WORK/bridge_reject.json" -w '%{http_code}' -X POST "$BRIDGE/bridge" \
  -H 'Content-Type: application/json' --data-binary @"$WORK/bad_0.json")
[ "$code" = "400" ] || fail "bridge accepted a corrupted packet (http $code)"
grep -q INVALID_SIGNATURE "$WORK/bridge_reject.json" \
  || fail "bridge did not report INVALID_SIGNATURE"
pass "bridge rejected a corrupted packet with INVALID_SIGNATURE"

# --- 4. Settlement verifies independently ------------------------------------
echo
echo "[4] settlement rejects corrupted packets published straight to the queue"
before_invalid=$(metric invalid_signature)
before_settled=$(metric settled)

"$PY" - <<PY
import asyncio, aio_pika

env = {}
for line in open(".env"):
    if "=" in line and not line.startswith("#"):
        k, v = line.rstrip("\n").split("=", 1)
        env[k] = v.strip('"')

url = f"amqp://{env['RABBITMQ_USER']}:{env['RABBITMQ_PASSWORD']}@localhost:15672/"
queue = env["SETTLEMENT_QUEUE"]

async def main() -> None:
    conn = await aio_pika.connect_robust(url)
    channel = await conn.channel(publisher_confirms=True)
    await channel.declare_queue(queue, durable=True)
    for i in range($CORRUPTED):
        body = open(f"$WORK/bad_{i}.json", "rb").read()
        await channel.default_exchange.publish(
            aio_pika.Message(body=body, content_type="application/json",
                             delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            routing_key=queue,
        )
    await conn.close()

asyncio.run(main())
PY
sleep 4

after_invalid=$(metric invalid_signature)
after_settled=$(metric settled)
gained=$((after_invalid - before_invalid))
[ "$gained" = "$CORRUPTED" ] \
  || fail "settlement logged $gained invalid signatures, expected $CORRUPTED"
pass "settlement independently rejected all $CORRUPTED corrupted packets"
[ "$after_settled" = "$before_settled" ] \
  || fail "a corrupted packet settled: $before_settled -> $after_settled"
pass "no corrupted packet settled"

rejected_rows=$(psql_scalar "select count(*) from rejected_packets where reason='INVALID_SIGNATURE'")
[ "$rejected_rows" -ge "$CORRUPTED" ] || fail "rejections were not persisted"
pass "rejections persisted to rejected_packets ($rejected_rows rows)"

# --- Summary ------------------------------------------------------------------
echo
echo "=============================================================="
echo "final settlement rows : $(psql_scalar "select count(*) from settlements")"
echo "final metrics         : $(curl -s $SETTLEMENT/metrics)"
echo
echo "ALL CHECKS PASSED"
