#!/usr/bin/env bash
# policypal-secrets.sh - create PolicyPal's two Secrets in the current cluster, with random values, once.
#
# For the local k3d cluster (make policypal-secrets-local switches to it first). On AWS, External Secrets
# Operator makes these Secrets from SSM Parameter Store instead; k3d has no AWS identity, and giving it AWS
# keys would put long-lived credentials in Kubernetes. Values exist only in the cluster: they are never
# printed and never passed as command-line arguments (visible to other users in ps); printf is a builtin.
# Secrets that already exist are left alone: Postgres keeps the password it was first started with.
set -euo pipefail

NAMESPACE="policypal"

# Only when missing: the manifests own the namespace (and its labels) once they are applied. --save-config
# records it the way kubectl apply expects, so the first deploy doesn't warn about a missing annotation.
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE" --save-config >/dev/null

# The KEY=value lines of each Secret, generated only when it is created.
db_values() {
  # Hex only: the API builds its database URL from this password (base/api.yaml), with no escaping.
  printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)"
}
admin_values() {
  printf 'ADMIN_TOKEN=%s\n' "$(openssl rand -hex 24)"
}

# ensure_secret <name> <values function>
ensure_secret() {
  if kubectl -n "$NAMESPACE" get secret "$1" >/dev/null 2>&1; then
    echo "policypal-secrets: $1 exists, left unchanged"
  else
    "$2" | kubectl -n "$NAMESPACE" create secret generic "$1" --from-env-file=/dev/stdin >/dev/null
    echo "policypal-secrets: $1 created"
  fi
}

ensure_secret policypal-db db_values
ensure_secret policypal-admin admin_values
