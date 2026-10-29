#!/bin/bash
set -euo pipefail

# Validates and unit-tests the Prometheus alert rules with promtool.
# The rules live in a PrometheusRule (a Kubernetes resource), but promtool needs a plain rules file,
# so the rule groups are extracted with yq first. Only Docker is required.

echo "=== Testing Prometheus alert rules ==="

RULES_DIR="$(cd "$(dirname "$0")/../monitoring/rules" && pwd)"
PROMETHEUS_IMAGE="prom/prometheus:v3.15.0" # same Prometheus version as kube-prometheus-stack 92.16.0
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
