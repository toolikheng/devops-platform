#!/usr/bin/env bash
# render-k8s.sh - print the PolicyPal manifests for one overlay (local or aws) as YAML on stdout.
#
# Usage: render-k8s.sh local|aws [POLICYPAL_COMMIT]
#
# Uses a pinned kustomize image, so the checks (ci/k8s-checks.sh, tests/test_k8s_policy.py) and the
# deploy (deploy-policypal.sh) all see exactly the same YAML, locally and in CI. Only Docker is needed.
# With a PolicyPal commit (40 lowercase hex characters: the image tag PolicyPal's CI publishes), a two-line
# kustomization on top of the overlay sets that tag with kustomize's own images field, so deploy.yml can
# deploy any commit; the repo's manifests are read as they are, read-only.
set -euo pipefail

KUSTOMIZE_IMAGE="registry.k8s.io/kustomize/kustomize:v5.8.3"
IMAGE="ghcr.io/toolikheng/policypal"
K8S_DIR="$(cd "$(dirname "$0")/../k8s" && pwd)"

overlay="${1:-}"
case "$overlay" in
  local|aws) ;;
  *) echo "usage: render-k8s.sh local|aws [POLICYPAL_COMMIT]" >&2; exit 2 ;;
esac
tag="${2:-}"
if [ "$#" -gt 1 ] && ! [[ "$tag" =~ ^[0-9a-f]{40}$ ]]; then
  echo "render-k8s.sh: POLICYPAL_COMMIT must be a full commit hash (40 lowercase hex characters)" >&2
  exit 2
fi

if [ -z "$tag" ]; then
  exec docker run --rm -v "$K8S_DIR:/k8s:ro" "$KUSTOMIZE_IMAGE" build "/k8s/policypal/overlays/$overlay"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
chmod 755 "$work" # the kustomize container's user must be able to read it
printf 'resources:\n- ../k8s/policypal/overlays/%s\nimages:\n- name: %s\n  newTag: "%s"\n' \
  "$overlay" "$IMAGE" "$tag" >"$work/kustomization.yaml"
docker run --rm -v "$K8S_DIR:/k8s:ro" -v "$work:/tag:ro" "$KUSTOMIZE_IMAGE" build /tag
