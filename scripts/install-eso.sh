#!/usr/bin/env bash
# install-eso.sh - install External Secrets Operator in kubectl's current cluster, then its ClusterSecretStore.
#
# Same chart, version and values as the checks (k8s/platform/external-secrets: chart.env, values.yaml).
# helm --wait returns once the controller, webhook and cert-controller are ready. The store comes after:
# its CRD and the webhook that validates it are part of the chart. make cloud-addons runs this against the
# AWS node (through make cloud-tunnel). Installing again is safe: helm upgrades in place.
set -euo pipefail

NAMESPACE="external-secrets"
ESO_DIR="$(cd "$(dirname "$0")/../k8s/platform/external-secrets" && pwd)"
# shellcheck source=k8s/platform/external-secrets/chart.env
source "$ESO_DIR/chart.env"

helm upgrade --install external-secrets "$ESO_CHART" --repo "$ESO_CHART_REPO" --version "$ESO_CHART_VERSION" \
  --namespace "$NAMESPACE" --create-namespace --values "$ESO_DIR/values.yaml" --wait --timeout 5m
kubectl apply -f "$ESO_DIR/clustersecretstore.yaml"
