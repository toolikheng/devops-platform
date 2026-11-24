#!/bin/bash
set -euo pipefail

# Validates and unit-tests the Prometheus alert rules with promtool.
# The rules live in a PrometheusRule (a Kubernetes resource), but promtool needs a plain rules file,
# so the rule groups are extracted with yq first. Only Docker is required.

echo "=== Testing Prometheus alert rules ==="

RULES_DIR="$(cd "$(dirname "$0")/../monitoring/rules" && pwd)"
# The image kube-prometheus-stack runs (k8s/platform/kube-prometheus-stack/chart.env); it carries promtool.
# tests/test_monitoring_platform.py checks they are the same, so a chart bump fails until this follows it.
PROMETHEUS_IMAGE="quay.io/prometheus/prometheus:v3.15.1-distroless"
YQ_IMAGE="mikefarah/yq:4.54.1"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

docker run --rm -v "$RULES_DIR:/in:ro" "$YQ_IMAGE" '{"groups": .spec.groups}' /in/alert-rules.yaml \
  > "$WORK/alert-rules.yaml"
cp "$RULES_DIR/alert-rules.test.yaml" "$WORK/"

promtool() {
  docker run --rm -u "$(id -u):$(id -g)" -v "$WORK:/w:ro" -w /w --entrypoint promtool "$PROMETHEUS_IMAGE" "$@"
}
promtool check rules alert-rules.yaml
promtool test rules alert-rules.test.yaml

echo "✓ Alert rules are valid and all rule tests passed"
