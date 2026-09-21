#!/usr/bin/env bash
# Create the settlement Secret in the current Kubernetes context from .env.
#
# Reads the same .env the compose stack uses, so the keys in the cluster match
# the ones the sender signs with. Nothing is written to disk and no secret is
# echoed: values go straight from .env into `kubectl create secret`.
#
# Deliberately passes only the two keys settlement needs:
#   SENDER_SIGNING_PUBLIC_KEY_PEM    to verify signatures
#   SETTLEMENT_RSA_PRIVATE_KEY_PEM   to unwrap the AES content key
# The sender's SIGNING PRIVATE key is never sent to the cluster. Settlement has
# no business being able to mint packets.
#
# Usage: ./scripts/k8s_secret.sh [NAMESPACE]

set -euo pipefail

NAMESPACE=${1:-default}
SECRET_NAME=meshsettle-settlement-secrets
ENV_FILE=${ENV_FILE:-.env}

[ -f "$ENV_FILE" ] || {
  echo "$ENV_FILE not found. Run: python scripts/bootstrap_env.py" >&2
  exit 1
}

# Read a value from .env, stripping surrounding quotes and turning the stored
# \n escapes back into real newlines (which is what a PEM needs).
read_env() {
  python3 - "$1" "$ENV_FILE" <<'PY'
import sys

key, path = sys.argv[1], sys.argv[2]
for line in open(path):
    if line.startswith(f"{key}="):
        value = line.rstrip("\n").split("=", 1)[1]
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        sys.stdout.write(value.replace("\\n", "\n"))
        break
else:
    sys.exit(f"{key} not found in {path}")
PY
}

echo "creating secret/$SECRET_NAME in namespace $NAMESPACE from $ENV_FILE"

kubectl create secret generic "$SECRET_NAME" \
  --namespace "$NAMESPACE" \
  --from-literal=POSTGRES_USER="$(read_env POSTGRES_USER)" \
  --from-literal=POSTGRES_PASSWORD="$(read_env POSTGRES_PASSWORD)" \
  --from-literal=RABBITMQ_USER="$(read_env RABBITMQ_USER)" \
  --from-literal=RABBITMQ_PASSWORD="$(read_env RABBITMQ_PASSWORD)" \
  --from-literal=SENDER_SIGNING_PUBLIC_KEY_PEM="$(read_env SENDER_SIGNING_PUBLIC_KEY_PEM)" \
  --from-literal=SETTLEMENT_RSA_PRIVATE_KEY_PEM="$(read_env SETTLEMENT_RSA_PRIVATE_KEY_PEM)" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl label secret "$SECRET_NAME" --namespace "$NAMESPACE" --overwrite \
  app.kubernetes.io/name=meshsettle \
  app.kubernetes.io/component=settlement >/dev/null

echo "done. keys in the secret:"
kubectl get secret "$SECRET_NAME" --namespace "$NAMESPACE" \
  -o go-template='{{range $k, $v := .data}}  {{$k}}{{"\n"}}{{end}}'
