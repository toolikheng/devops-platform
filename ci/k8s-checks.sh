#!/bin/bash
set -euo pipefail

# Offline Kubernetes checks: renders each PolicyPal overlay with the pinned kustomize image, and External
# Secrets Operator's pinned chart with the pinned Helm image, and validates every object against the
# schemas of the node's Kubernetes version (ci/versions.env) with kubeconform (strict: unknown fields fail). ExternalSecret and
# ClusterSecretStore are custom resources, so their schemas come from the CRDs catalog at a pinned commit.
# The project rules on the rendered YAML (probes, limits, image tags, no Secrets, ESO on the host network,
# ...) are pytest tests in tests/, run by make test-infra. Only Docker is needed; kubeconform downloads the
# schemas from GitHub.

echo "=== Running Kubernetes checks ==="

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBECONFORM_IMAGE="ghcr.io/yannh/kubeconform:v0.8.0"
# KUBERNETES_VERSION: the version of k3s on the AWS node.
# shellcheck source=ci/versions.env
source "$REPO_ROOT/ci/versions.env"
CRD_SCHEMAS="https://raw.githubusercontent.com/datreeio/CRDs-catalog/034f273c7ab88c24f01d828db4dfc95e86f5b3a4/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"

kubeconform() {
  docker run --rm -i "$KUBECONFORM_IMAGE" -strict -summary -kubernetes-version "$KUBERNETES_VERSION" \
    -schema-location default -schema-location "$CRD_SCHEMAS" "$@" -
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
