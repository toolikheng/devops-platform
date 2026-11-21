#!/bin/bash
set -euo pipefail

# Offline Kubernetes checks for PolicyPal: renders each overlay with the pinned kustomize image and
# validates every object against the Kubernetes 1.36 schemas with kubeconform (strict: unknown fields
# fail). The project rules on the rendered YAML (probes, limits, image tags, no Secrets, ...) are
# pytest tests in tests/test_k8s_policy.py, run by make test-infra. Only Docker is needed; kubeconform
# downloads the schemas from GitHub.

echo "=== Running Kubernetes checks ==="

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBECONFORM_IMAGE="ghcr.io/yannh/kubeconform:v0.8.0"
KUBERNETES_VERSION="1.36.6" # same version as k3s on the AWS node (ansible/roles/k3s/defaults/main.yml; a test checks)

for overlay in local aws; do
  echo "--- k8s/policypal/overlays/$overlay: kustomize build | kubeconform"
  bash "$REPO_ROOT/scripts/render-k8s.sh" "$overlay" |
    docker run --rm -i "$KUBECONFORM_IMAGE" -strict -summary \
      -kubernetes-version "$KUBERNETES_VERSION" -
done

echo "✓ Kubernetes checks passed"
