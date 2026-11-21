#!/usr/bin/env bash
# deploy-policypal.sh - deploy PolicyPal to the current cluster, wait for it, smoke-test it, and roll back
# what this deploy changed if any step fails.
#
# Usage: deploy-policypal.sh local|aws <ui-url>
#
# Applies exactly the YAML that the checks validated (render-k8s.sh, pinned kustomize). Before applying it
# records each Deployment's revision; on failure, every Deployment whose revision changed goes back to the
# one it had. Postgres is never rolled back (its data volume stays as it is). A first deploy has nothing to
# roll back to. The Secrets must exist first (policypal-secrets.sh). ROLLOUT_TIMEOUT allows for the first
# pull of the ~2.8 GB image; the Deployments' progressDeadlineSeconds must be at least as long, or Kubernetes
# gives up first (a test checks).
set -euo pipefail

NAMESPACE="policypal"
DEPLOYMENTS=(policypal-api policypal-ui)
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-15m}"
here="$(dirname "$0")"

overlay="${1:-}"
ui="${2:-}"
if ! [[ "$overlay" =~ ^(local|aws)$ && "$ui" =~ ^https?://[^/]+ ]]; then
  echo "usage: deploy-policypal.sh local|aws <ui-url>" >&2
  exit 2
fi

k() { kubectl -n "$NAMESPACE" "$@"; }
revision() {
  k get deployment "$1" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null || true
}

if ! k get secret policypal-db policypal-admin >/dev/null 2>&1; then
  echo "deploy-policypal: Secrets policypal-db and policypal-admin are missing; run scripts/policypal-secrets.sh first." >&2
  exit 1
fi

declare -A before
for d in "${DEPLOYMENTS[@]}"; do before[$d]="$(revision "$d")"; done

rollback() {
  local d now
  for d in "${DEPLOYMENTS[@]}"; do
    now="$(revision "$d")"
    if [ -n "${before[$d]}" ] && [ "$now" != "${before[$d]}" ]; then
      echo "deploy-policypal: rolling deployment/$d back to revision ${before[$d]}" >&2
      k rollout undo "deployment/$d" --to-revision="${before[$d]}"
      k rollout status "deployment/$d" --timeout="$ROLLOUT_TIMEOUT" || true
    fi
  done
}

manifests="$(bash "$here/render-k8s.sh" "$overlay")"

failed=""
if ! kubectl apply -f - <<<"$manifests"; then
  failed="kubectl apply"
else
  for target in statefulset/postgres deployment/policypal-api deployment/policypal-ui; do
    if ! k rollout status "$target" --timeout="$ROLLOUT_TIMEOUT"; then
      failed="rollout of $target"
      break
    fi
  done
fi
if [ -z "$failed" ] && ! bash "$here/smoke-test.sh" "$ui"; then
  failed="smoke test"
fi

if [ -n "$failed" ]; then
  echo "deploy-policypal: $failed failed" >&2
  k get pods -o wide >&2 || true
  rollback
  exit 1
fi
echo "deploy-policypal: $overlay deployed and smoke-tested"
