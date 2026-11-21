#!/usr/bin/env bash
# policypal-secrets.sh - create PolicyPal's two Secrets in the current cluster, with random values, once.
#
# Temporary until Phase C5, where External Secrets Operator creates them from SSM Parameter Store. Runs
# against kubectl's current cluster: make policypal-secrets-local switches to k3d first, make cloud-deploy
# sets KUBECONFIG to the AWS cluster's file. Values exist only in the cluster: they are never printed and
# never passed as command-line arguments (visible to other users in ps); printf is a shell builtin.
# Secrets that already exist are left alone: Postgres keeps the password it was first started with.
set -euo pipefail

NAMESPACE="policypal"

# Only when missing: the manifests own the namespace (and its labels) once they are applied.
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE" >/dev/null

# create_secret <name>: the KEY=value lines come in on stdin.
create_secret() {
  if kubectl -n "$NAMESPACE" get secret "$1" >/dev/null 2>&1; then
    cat >/dev/null
    echo "policypal-secrets: $1 exists, left unchanged"
  else
    kubectl -n "$NAMESPACE" create secret generic "$1" --from-env-file=/dev/stdin >/dev/null
    echo "policypal-secrets: $1 created"
  fi
}

# Hex only, so the password needs no escaping inside the database URL.
password="$(openssl rand -hex 24)"
printf 'POSTGRES_PASSWORD=%s\nDATABASE_URL=postgresql+psycopg://policypal:%s@postgres:5432/policypal\n' \
  "$password" "$password" | create_secret policypal-db
printf 'ADMIN_TOKEN=%s\n' "$(openssl rand -hex 24)" | create_secret policypal-admin
