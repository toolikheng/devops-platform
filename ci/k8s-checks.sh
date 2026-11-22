#!/bin/bash
set -euo pipefail

# Offline Kubernetes checks: renders each PolicyPal overlay with the pinned kustomize image, and External
# Secrets Operator's pinned chart with the pinned Helm image, and validates every object with
# ci/kubeconform.sh (strict, against the node's Kubernetes version). The project rules on the rendered YAML
# (probes, limits, image tags, no Secrets, ESO on the host network, ...) are pytest tests in tests/, run by
# make test-infra. Only Docker is needed.

echo "=== Running Kubernetes checks ==="

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

kubeconform() {
  bash "$REPO_ROOT/ci/kubeconform.sh" "$@"
}

for overlay in local aws; do
  echo "--- k8s/policypal/overlays/$overlay: kustomize build | kubeconform"
  bash "$REPO_ROOT/scripts/render-k8s.sh" "$overlay" | kubeconform
done

# The chart's 25 CustomResourceDefinitions are skipped: neither schema source has a schema for the
# CustomResourceDefinition kind itself. They are the chart's own, applied unchanged, and the API server
# validates them when they are installed. Everything else in the chart is validated.
echo "--- k8s/platform/external-secrets: helm template + ClusterSecretStore | kubeconform"
{
  bash "$REPO_ROOT/scripts/render-eso.sh"
  echo "---"
  cat "$REPO_ROOT/k8s/platform/external-secrets/clustersecretstore.yaml"
} | kubeconform -skip CustomResourceDefinition

echo "✓ Kubernetes checks passed"
