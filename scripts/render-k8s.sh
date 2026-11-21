#!/usr/bin/env bash
# render-k8s.sh - print the PolicyPal manifests for one overlay (local or aws) as YAML on stdout.
#
# Uses a pinned kustomize image, so the checks (ci/k8s-checks.sh, tests/test_k8s_policy.py) and the
# deploy (deploy-policypal.sh) all see exactly the same YAML, locally and in CI. Only Docker is needed.
set -euo pipefail

KUSTOMIZE_IMAGE="registry.k8s.io/kustomize/kustomize:v5.8.3"
K8S_DIR="$(cd "$(dirname "$0")/../k8s" && pwd)"

overlay="${1:-}"
case "$overlay" in
  local|aws) ;;
  *) echo "usage: render-k8s.sh local|aws" >&2; exit 2 ;;
esac

docker run --rm -v "$K8S_DIR:/k8s:ro" "$KUSTOMIZE_IMAGE" build "/k8s/policypal/overlays/$overlay"
