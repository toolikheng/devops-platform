#!/usr/bin/env bash
# render-k8s.sh - print the PolicyPal manifests for one overlay (local or aws) as YAML on stdout.
#
# Usage: render-k8s.sh local|aws [POLICYPAL_COMMIT]
#
# Uses a pinned kustomize image, so the checks (ci/k8s-checks.sh, tests/test_k8s_policy.py) and the
# deploy (deploy-policypal.sh) all see exactly the same YAML, locally and in CI. Only Docker is needed.
# With a PolicyPal commit (40 lowercase hex characters: the image tag PolicyPal's CI publishes), the
# overlay's pinned tag is replaced in a temporary copy, so deploy.yml can deploy any commit and the repo
# stays as it is.
set -euo pipefail

KUSTOMIZE_IMAGE="registry.k8s.io/kustomize/kustomize:v5.8.3"
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

dir="$K8S_DIR"
if [ -n "$tag" ]; then
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  chmod 755 "$work" # the kustomize container's user must be able to read the copy
  cp -R "$K8S_DIR" "$work/k8s"
  kustomization="$work/k8s/policypal/overlays/$overlay/kustomization.yaml"
  pinned='^  newTag: "[0-9a-f]{40}"$'
  # Exactly one pinned tag, as the overlays have: anything else is a layout this script doesn't know.
  if [ "$(grep -c -E "$pinned" "$kustomization")" != 1 ]; then
    echo "render-k8s.sh: expected one quoted 40-character newTag in overlays/$overlay/kustomization.yaml" >&2
    exit 1
  fi
  sed -i -E "s/$pinned/  newTag: \"$tag\"/" "$kustomization"
  dir="$work/k8s"
fi

docker run --rm -v "$dir:/k8s:ro" "$KUSTOMIZE_IMAGE" build "/k8s/policypal/overlays/$overlay"
