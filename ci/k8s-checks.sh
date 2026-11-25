#!/bin/bash
set -euo pipefail

# Offline Kubernetes checks: renders each PolicyPal overlay with the pinned kustomize image, and every
# platform add-on (k8s/platform/NAME, one run per values-ENV.yaml) with scripts/addon.sh's pinned Helm image,
# and validates every object with ci/kubeconform.sh (strict, against the node's Kubernetes version). The
# project rules on the rendered YAML (probes, limits, image tags, no Secrets, ESO on the host network, ...) are
# pytest tests in tests/, run by make test-infra. Only Docker is needed.

echo "=== Running Kubernetes checks ==="

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

kubeconform() {
  bash "$REPO_ROOT/ci/kubeconform.sh" "$@"
}

for overlay in local aws; do
  echo "--- k8s/policypal/overlays/$overlay: kustomize build | kubeconform"
  bash "$REPO_ROOT/scripts/render-k8s.sh" "$overlay" | kubeconform
done

# A chart that ships its CustomResourceDefinitions as templates (External Secrets Operator: 25) renders them,
# and neither schema source has a schema for the CustomResourceDefinition kind itself. They are the chart's
# own, applied unchanged, and the API server validates them when they are installed. Everything else in each
# chart, and the manifests applied after it, is validated.
for values in "$REPO_ROOT"/k8s/platform/*/values-*.yaml; do
  name="$(basename "$(dirname "$values")")"
  env="$(basename "$values" .yaml)"
  env="${env#values-}"
  echo "--- k8s/platform/$name ($env): helm template + manifests | kubeconform"
  bash "$REPO_ROOT/scripts/addon.sh" render "$name" "$env" | kubeconform -skip CustomResourceDefinition
done

echo "✓ Kubernetes checks passed"
