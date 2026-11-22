#!/usr/bin/env bash
# deploy-policypal.sh - deploy PolicyPal to the current cluster, wait for it, smoke-test it, and roll back
# what this deploy changed if any step fails.
#
# Usage: deploy-policypal.sh local|aws <ui-url>
#
# Applies exactly the YAML that the checks validated (render-k8s.sh, pinned kustomize). Before applying it
# records each Deployment's revision; on failure, every Deployment whose revision changed goes back to the
# one it had. Postgres is never rolled back (its data volume stays as it is). A first deploy has nothing to
# roll back to. After the apply it waits up to 2 minutes for the Secrets the pods read: on AWS, External
# Secrets Operator makes them from the ExternalSecrets in this apply; on k3d, policypal-secrets.sh made them
# before. ROLLOUT_TIMEOUT allows for the first pull of the ~2.8 GB image; the Deployments'
# progressDeadlineSeconds must be at least as long, or Kubernetes gives up first (a test checks).
set -euo pipefail

NAMESPACE="policypal"
DEPLOYMENTS=(policypal-api policypal-ui)
SECRETS=(policypal-db policypal-admin) # every Secret the manifests reference (a test checks)
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-15m}"
here="$(dirname "$0")"

overlay="${1:-}"
ui="${2:-}"
if ! [[ "$overlay" =~ ^(local|aws)$ && "$ui" =~ ^https?://[^/]+ ]]; then
  echo "usage: deploy-policypal.sh local|aws <ui-url>" >&2
  exit 2
fi

k() { kubectl -n "$NAMESPACE" "$@"; }
# One kubectl call per Secret: --for=create only waits when it is given a single resource; given several,
# it fails at once if any is missing. Stops at the first Secret that doesn't appear, so 2 minutes at most.
secrets_created() {
  local s
  for s in "${SECRETS[@]}"; do
    k wait --for=create "secret/$s" --timeout=2m || return 1
  done
}
revision() {
  k get deployment "$1" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null || true
}

declare -A before
for d in "${DEPLOYMENTS[@]}"; do before[$d]="$(revision "$d")"; done

rollback() {
  local d now
  for d in "${DEPLOYMENTS[@]}"; do
    now="$(revision "$d")"
    if [ -n "${before[$d]}" ] && [ "$now" != "${before[$d]}" ]; then
      echo "deploy-policypal: rolling deployment/$d back to revision ${before[$d]}" >&2
      # A failed undo must not stop the others (set -e would): say so and go on.
      if k rollout undo "deployment/$d" --to-revision="${before[$d]}"; then
        k rollout status "deployment/$d" --timeout="$ROLLOUT_TIMEOUT" || true
      else
        echo "deploy-policypal: rolling deployment/$d back failed; check it with kubectl rollout history" >&2
      fi
    fi
  done
}

manifests="$(bash "$here/render-k8s.sh" "$overlay")"

failed=""
if ! kubectl apply -f - <<<"$manifests"; then
  failed="kubectl apply"
elif ! secrets_created; then
  failed="waiting for the Secrets ${SECRETS[*]}"
  echo "deploy-policypal: on AWS see kubectl -n $NAMESPACE describe externalsecret (External Secrets Operator);" \
    "on k3d run scripts/policypal-secrets.sh" >&2
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
