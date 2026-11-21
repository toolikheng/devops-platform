#!/usr/bin/env bash
# render-eso.sh - print External Secrets Operator's chart, rendered with our values, as YAML on stdout.
#
# The chart and its version come from k8s/platform/external-secrets/chart.env, which install-eso.sh also
# reads, so the checks (ci/k8s-checks.sh, tests/test_eso_platform.py) see what the cluster gets. Uses a
# pinned Helm image: only Docker is needed, and the network to download the chart.
set -euo pipefail

HELM_IMAGE="alpine/helm:3.21.4"
KUBERNETES_VERSION="1.36.6" # same version as k3s on the AWS node (ansible/roles/k3s/defaults/main.yml; a test checks)
ESO_DIR="$(cd "$(dirname "$0")/../k8s/platform/external-secrets" && pwd)"
# shellcheck source=k8s/platform/external-secrets/chart.env
source "$ESO_DIR/chart.env"

docker run --rm -v "$ESO_DIR:/eso:ro" "$HELM_IMAGE" \
  template external-secrets "$ESO_CHART" --repo "$ESO_CHART_REPO" --version "$ESO_CHART_VERSION" \
  --namespace external-secrets --kube-version "$KUBERNETES_VERSION" --values /eso/values.yaml
