#!/usr/bin/env bash
# Verify the Kubernetes manifests against a real local cluster (kind).
#
# Creates a throwaway kind cluster, installs metrics-server so the CPU-based HPA
# has something to read, loads the locally built settlement image, applies the
# manifests, and asserts that:
#
#   1. every manifest is accepted by the API server
#   2. the migration Job completes
#   3. the Deployment rolls out and both replicas become Ready
#   4. the Service resolves and the read API answers through it
#   5. the settlement pods are actually consuming from the queue
#   6. the HPA acquires real metrics and reports targets (not <unknown>)
#   7. a payment published to the in-cluster queue settles in the in-cluster DB
#
# Usage:
#   docker compose build                # or build the settlement image directly
#   ./scripts/verify_k8s.sh
#   ./scripts/verify_k8s.sh --keep      # leave the cluster running afterwards
#
# Requires: kind, kubectl, a built meshsettle/settlement:local image, and .env.

set -euo pipefail

CLUSTER=${CLUSTER:-meshsettle}
IMAGE=${IMAGE:-meshsettle/settlement:local}
PROBE_IMAGE=${PROBE_IMAGE:-curlimages/curl:8.11.1}
KEEP=false
[ "${1:-}" = "--keep" ] && KEEP=true

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; exit 1; }

cleanup() {
  if [ "$KEEP" = true ]; then
    echo
    echo "leaving cluster '$CLUSTER' running (--keep). Delete it with:"
    echo "  kind delete cluster --name $CLUSTER"
  else
    echo
    echo "deleting kind cluster '$CLUSTER'..."
    kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  fi
}

echo "MeshSettle Kubernetes verification"
echo "UTC        : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "kind       : $(kind version)"
echo "kubectl    : $(kubectl version --client -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clientVersion"]["gitVersion"])')"
echo "=============================================================="

command -v kind >/dev/null || fail "kind is not installed"
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || fail "$IMAGE not found locally; run: docker compose build settlement"
[ -f .env ] || fail ".env not found; run: python scripts/bootstrap_env.py"

# --- 0. YAML is parseable (no cluster needed) ---------------------------------
echo
echo "[0] YAML parses"
for manifest in k8s/*.yaml; do
  python3 -c "
import sys, yaml
docs = list(yaml.safe_load_all(open(sys.argv[1])))
assert docs, 'no documents'
for d in docs:
    if d is None:
        continue
    assert 'apiVersion' in d and 'kind' in d, f'missing apiVersion/kind in {sys.argv[1]}'
" "$manifest" 2>/dev/null || fail "$manifest is not parseable YAML"
  pass "$(basename "$manifest") parses"
done

# --- 1. Cluster ----------------------------------------------------------------
echo
echo "[1] creating kind cluster '$CLUSTER'"
kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
kind create cluster --name "$CLUSTER" --wait 120s
trap cleanup EXIT
kubectl config use-context "kind-$CLUSTER" >/dev/null
pass "cluster up"

# Schema validation needs a live API server, so it happens here rather than
# before the cluster exists. Server-side dry-run is stronger than client-side:
# it runs admission and full schema validation without persisting anything.
echo
echo "[1b] server-side validation of every manifest"
for manifest in k8s/*.yaml; do
  case "$manifest" in
    *secret.example.yaml) continue ;;  # template, placeholder values only
    *dependencies.yaml) continue ;;    # references the Secret, applied later
    *settlement-hpa.yaml) continue ;;  # references the Deployment, applied later
  esac
  kubectl apply --dry-run=server -f "$manifest" >/dev/null \
    || fail "$manifest was rejected by the API server"
  pass "$(basename "$manifest") accepted by the API server"
done

# --- 2. metrics-server (so the HPA is real, not decorative) -------------------
echo
echo "[2] installing metrics-server"
# Fetching from GitHub is the one step here that depends on the network, and it
# does intermittently return 504. Retry, and fall back to a pinned version so a
# transient failure on the "latest" redirect does not fail the whole run.
METRICS_URLS=(
  "https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
  "https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml"
)
INSTALLED=false
for url in "${METRICS_URLS[@]}"; do
  for attempt in 1 2 3; do
    if kubectl apply -f "$url" >/dev/null 2>&1; then
      INSTALLED=true
      break 2
    fi
    echo "  attempt $attempt for $(basename "$(dirname "$url")") failed, retrying..."
    sleep 5
  done
done
[ "$INSTALLED" = true ] || fail "could not install metrics-server (network?)"
# kind nodes serve kubelet metrics over a self-signed cert, so metrics-server
# needs to be told to accept it. Without this it never becomes ready and the
# HPA reports <unknown> forever.
kubectl patch deployment metrics-server -n kube-system --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null
kubectl rollout status deployment/metrics-server -n kube-system --timeout=180s >/dev/null \
  || fail "metrics-server did not become ready"
pass "metrics-server ready"

# --- 3. Image and secrets ------------------------------------------------------
echo
echo "[3] loading images and creating the secret"
kind load docker-image "$IMAGE" --name "$CLUSTER" >/dev/null
pass "settlement image loaded into the cluster"

# Preload the dependency images too. A kind node has its own image store, so
# without this every pod pulls from Docker Hub inside the node, which is slow
# enough to look like a broken manifest (it was: postgres sat in
# ContainerCreating past a 180s wait purely on pull time). These images are
# already on the host from the compose stack, so sideload them.
# Best effort: `kind load docker-image` fails for multi-platform manifests held
# in Docker Desktop's image store ("content digest ... not found"), so fall back
# to a saved archive, and if that also fails just let the node pull the image
# itself. Preloading is an optimization, not a requirement, which is why the
# waits below are generous enough to cover a cold pull.
DEP_IMAGES=(postgres:16-alpine redis:7-alpine rabbitmq:3.13 "$PROBE_IMAGE")
for dep in "${DEP_IMAGES[@]}"; do
  docker image inspect "$dep" >/dev/null 2>&1 || docker pull -q "$dep" >/dev/null 2>&1 || true
  if kind load docker-image "$dep" --name "$CLUSTER" >/dev/null 2>&1; then
    pass "$dep preloaded"
  else
    archive=$(mktemp -t meshsettle-img).tar
    if docker save "$dep" -o "$archive" 2>/dev/null \
      && kind load image-archive "$archive" --name "$CLUSTER" >/dev/null 2>&1; then
      pass "$dep preloaded (via archive)"
    else
      echo "  NOTE  $dep could not be preloaded; the node will pull it"
    fi
    rm -f "$archive"
  fi
done

./scripts/k8s_secret.sh default >/dev/null
pass "secret created from .env"

# --- 4. Apply ------------------------------------------------------------------
echo
echo "[4] applying manifests"
kubectl apply -f k8s/configmap.yaml >/dev/null
kubectl apply -f k8s/dependencies.yaml >/dev/null
pass "configmap and dependencies applied"

echo "  waiting for dependencies to be ready (cold image pulls + RabbitMQ boot)..."
for dep in postgres redis rabbitmq; do
  kubectl wait --for=condition=available "deployment/meshsettle-$dep" --timeout=600s >/dev/null \
    || fail "meshsettle-$dep never became available: $(kubectl describe deployment "meshsettle-$dep" 2>&1 | tail -5)"
  pass "$dep ready"
done

kubectl apply -f k8s/settlement-deployment.yaml >/dev/null
kubectl apply -f k8s/settlement-service.yaml >/dev/null
kubectl apply -f k8s/settlement-hpa.yaml >/dev/null
pass "settlement deployment, service and hpa applied"

# --- 5. Migration Job ----------------------------------------------------------
echo
echo "[5] migration Job"
kubectl wait --for=condition=complete job/meshsettle-migrate --timeout=180s >/dev/null \
  || fail "migration Job did not complete: $(kubectl logs job/meshsettle-migrate --tail=5 2>&1)"
pass "alembic upgrade head completed in-cluster"

# --- 6. Rollout ----------------------------------------------------------------
echo
echo "[6] settlement rollout"
kubectl rollout status deployment/meshsettle-settlement --timeout=240s >/dev/null \
  || fail "settlement did not roll out"
READY=$(kubectl get deployment meshsettle-settlement -o jsonpath='{.status.readyReplicas}')
[ "$READY" = "2" ] || fail "expected 2 ready replicas, got ${READY:-0}"
pass "$READY replicas ready"

# --- 7. Service and API --------------------------------------------------------
echo
echo "[7] read API through the Service"
HEALTH=$(kubectl run meshsettle-probe --rm -i --restart=Never \
  --image="$PROBE_IMAGE" --image-pull-policy=IfNotPresent -- \
  -sS --max-time 10 http://meshsettle-settlement:8004/healthz 2>/dev/null | tail -1)
echo "  $HEALTH"
echo "$HEALTH" | grep -q '"status":"ok"' || fail "health through the Service was not ok"
pass "Service resolves and the API answers"
echo "$HEALTH" | grep -q '"consuming":true' || fail "pods are not consuming from the queue"
pass "pods are consuming from the queue"
echo "$HEALTH" | grep -q '"database":true' || fail "pods cannot reach Postgres"
echo "$HEALTH" | grep -q '"redis":true' || fail "pods cannot reach Redis"
pass "pods can reach Postgres and Redis"

# --- 8. HPA --------------------------------------------------------------------
echo
echo "[8] HPA acquires real metrics"
HPA_OK=false
for _ in $(seq 1 40); do
  TARGETS=$(kubectl get hpa meshsettle-settlement -o jsonpath='{.spec.metrics[*].resource.name}' 2>/dev/null)
  CURRENT=$(kubectl get hpa meshsettle-settlement \
    -o jsonpath='{.status.currentMetrics[*].resource.current.averageUtilization}' 2>/dev/null)
  if [ -n "$CURRENT" ]; then
    echo "  metrics: $TARGETS -> current utilization: $CURRENT"
    HPA_OK=true
    break
  fi
  sleep 5
done
[ "$HPA_OK" = true ] || fail "HPA never reported current metrics (still <unknown>)"
pass "HPA is reading live CPU and memory metrics"

ABLE=$(kubectl get hpa meshsettle-settlement \
  -o jsonpath='{.status.conditions[?(@.type=="ScalingActive")].status}')
[ "$ABLE" = "True" ] || fail "HPA ScalingActive is $ABLE"
pass "HPA ScalingActive=True"

kubectl get hpa meshsettle-settlement --no-headers | sed 's/^/  /'

# --- 9. A real payment settles in-cluster --------------------------------------
echo
echo "[9] publishing a signed packet to the in-cluster queue"
kubectl port-forward service/meshsettle-rabbitmq 15699:5672 >/dev/null 2>&1 &
PF_PID=$!
sleep 4

BEFORE=$(kubectl exec deployment/meshsettle-postgres -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from settlements"' | tr -d '[:space:]')

.venv/bin/python - <<'PY'
import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import aio_pika

from shared.config import normalize_pem, settings
from shared.crypto import load_rsa_public_key, load_signing_private_key
from shared.models import PaymentInstruction, PaymentPacket

signing_key = load_signing_private_key(normalize_pem(settings.sender_signing_private_key_pem))
rsa_public = load_rsa_public_key(normalize_pem(settings.settlement_rsa_public_key_pem))

instruction = PaymentInstruction(
    packet_id=uuid4(),
    payer_id="device-k8s-payer",
    payee_id="device-k8s-payee",
    amount_minor=31337,
    currency="INR",
    created_at=datetime.now(UTC),
)
packet = PaymentPacket.create(
    instruction=instruction,
    sender_id="device-k8s",
    signing_key=signing_key,
    recipient_public_key=rsa_public,
)

url = (
    f"amqp://{settings.rabbitmq_user}:{settings.rabbitmq_password}"
    f"@localhost:15699/"
)


async def main() -> None:
    conn = await aio_pika.connect_robust(url)
    channel = await conn.channel(publisher_confirms=True)
    await channel.declare_queue(settings.settlement_queue, durable=True)
    body = packet.model_dump_json().encode("utf-8")
    # Publish it three times: one must settle, two must be rejected as
    # duplicates, and the replicas are racing for it.
    for _ in range(3):
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=body,
                message_id=packet.idempotency_key,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=settings.settlement_queue,
        )
    await conn.close()
    print(json.dumps({"idempotency_key": packet.idempotency_key}))


asyncio.run(main())
PY

kill $PF_PID 2>/dev/null || true
sleep 6

AFTER=$(kubectl exec deployment/meshsettle-postgres -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from settlements"' | tr -d '[:space:]')
NEW=$((AFTER - BEFORE))
echo "  settlement rows: $BEFORE -> $AFTER"
[ "$NEW" = "1" ] || fail "3 copies of one packet produced $NEW settlements, expected exactly 1"
pass "3 duplicate copies across 2 replicas settled exactly once"

AMOUNT=$(kubectl exec deployment/meshsettle-postgres -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select amount_minor from settlements order by id desc limit 1"' | tr -d '[:space:]')
[ "$AMOUNT" = "31337" ] || fail "settled amount was $AMOUNT, expected 31337"
pass "settled the exact amount that was sealed ($AMOUNT)"

# --- 10. Exactly-once across scaled-out replicas -------------------------------
#
# This is the check that justifies the HPA existing at all. Scale to 5 replicas,
# publish 10 distinct packets 20 times each (200 messages), and require exactly
# 10 settlements. Five independent pods, each with its own Redis connection and
# its own database session, racing on the same keys.
echo
echo "[10] exactly-once across 5 replicas"
kubectl scale deployment/meshsettle-settlement --replicas=5 >/dev/null
kubectl rollout status deployment/meshsettle-settlement --timeout=240s >/dev/null \
  || fail "could not scale to 5 replicas"
pass "scaled to 5 replicas"

kubectl port-forward service/meshsettle-rabbitmq 15699:5672 >/dev/null 2>&1 &
PF_PID=$!
sleep 4

BEFORE_SCALE=$(kubectl exec deployment/meshsettle-postgres -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from settlements"' | tr -d '[:space:]')

DISTINCT=${DISTINCT:-10}
COPIES=${COPIES:-20}
.venv/bin/python - "$DISTINCT" "$COPIES" <<'PY'
import asyncio
import sys
from datetime import UTC, datetime
from uuid import uuid4

import aio_pika

from shared.config import normalize_pem, settings
from shared.crypto import load_rsa_public_key, load_signing_private_key
from shared.models import PaymentInstruction, PaymentPacket

distinct, copies = int(sys.argv[1]), int(sys.argv[2])
signing_key = load_signing_private_key(normalize_pem(settings.sender_signing_private_key_pem))
rsa_public = load_rsa_public_key(normalize_pem(settings.settlement_rsa_public_key_pem))
url = f"amqp://{settings.rabbitmq_user}:{settings.rabbitmq_password}@localhost:15699/"

packets = []
for index in range(distinct):
    instruction = PaymentInstruction(
        packet_id=uuid4(),
        payer_id=f"device-scale-p{index}",
        payee_id=f"device-scale-q{index}",
        amount_minor=1000 + index,
        currency="INR",
        created_at=datetime.now(UTC),
    )
    packets.append(
        PaymentPacket.create(
            instruction=instruction,
            sender_id="device-k8s",
            signing_key=signing_key,
            recipient_public_key=rsa_public,
        )
    )


async def main() -> None:
    conn = await aio_pika.connect_robust(url)
    channel = await conn.channel(publisher_confirms=True)
    await channel.declare_queue(settings.settlement_queue, durable=True)
    sent = 0
    for packet in packets:
        body = packet.model_dump_json().encode("utf-8")
        for _ in range(copies):
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=body,
                    message_id=packet.idempotency_key,
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=settings.settlement_queue,
            )
            sent += 1
    await conn.close()
    print(f"  published {sent} messages ({distinct} distinct x {copies} copies)")


asyncio.run(main())
PY

kill $PF_PID 2>/dev/null || true
sleep 12

AFTER_SCALE=$(kubectl exec deployment/meshsettle-postgres -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from settlements"' | tr -d '[:space:]')
NEW_SCALE=$((AFTER_SCALE - BEFORE_SCALE))
echo "  settlement rows: $BEFORE_SCALE -> $AFTER_SCALE (new: $NEW_SCALE, expected $DISTINCT)"
[ "$NEW_SCALE" = "$DISTINCT" ] \
  || fail "$((DISTINCT * COPIES)) messages produced $NEW_SCALE settlements, expected $DISTINCT"
pass "$((DISTINCT * COPIES)) messages across 5 replicas settled exactly $DISTINCT times"

DUP_KEYS=$(kubectl exec deployment/meshsettle-postgres -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from (select idempotency_key from settlements group by idempotency_key having count(*)>1) d"' | tr -d '[:space:]')
[ "$DUP_KEYS" = "0" ] || fail "$DUP_KEYS idempotency keys settled more than once"
pass "no idempotency key settled twice"

DUP_IDS=$(kubectl exec deployment/meshsettle-postgres -- sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from (select packet_id from settlements group by packet_id having count(*)>1) d"' | tr -d '[:space:]')
[ "$DUP_IDS" = "0" ] || fail "$DUP_IDS packet ids settled more than once"
pass "no packet id settled twice"

# --- Summary -------------------------------------------------------------------
echo
echo "=============================================================="
kubectl get deployment,service,hpa,job -l app.kubernetes.io/name=meshsettle --no-headers 2>/dev/null | sed 's/^/  /'
echo
echo "ALL KUBERNETES CHECKS PASSED"
