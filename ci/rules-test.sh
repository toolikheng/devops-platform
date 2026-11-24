#!/bin/bash
set -euo pipefail

# Validates and unit-tests the Prometheus alert rules with promtool: the homelab's (monitoring/rules) and
# PolicyPal's on AWS (k8s/policypal/overlays/aws). The rules live in PrometheusRules (Kubernetes resources), but
# promtool needs plain rules files, so the rule groups are extracted with yq first, under the names the test
# files load. tests/test_monitoring_platform.py checks that every PrometheusRule in the repo is listed here.
# Only Docker is required.

echo "=== Testing Prometheus alert rules ==="

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# The image kube-prometheus-stack runs (k8s/platform/kube-prometheus-stack/chart.env); it carries promtool.
# tests/test_monitoring_platform.py checks they are the same, so a chart bump fails until this follows it.
PROMETHEUS_IMAGE="quay.io/prometheus/prometheus:v3.15.1-distroless"
YQ_IMAGE="mikefarah/yq:4.54.1"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# extract <PrometheusRule file, relative to the repo> <rules file name the tests load>
extract() {
  docker run --rm -v "$REPO_ROOT:/repo:ro" "$YQ_IMAGE" '{"groups": .spec.groups}' "/repo/$1" > "$WORK/$2"
}
extract monitoring/rules/alert-rules.yaml alert-rules.yaml
extract k8s/policypal/overlays/aws/prometheusrule.yaml policypal-alerts.yaml
cp "$REPO_ROOT/monitoring/rules/alert-rules.test.yaml" "$REPO_ROOT/monitoring/rules/policypal-alerts.test.yaml" "$WORK/"

promtool() {
  docker run --rm -u "$(id -u):$(id -g)" -v "$WORK:/w:ro" -w /w --entrypoint promtool "$PROMETHEUS_IMAGE" "$@"
}
promtool check rules alert-rules.yaml policypal-alerts.yaml
promtool test rules alert-rules.test.yaml policypal-alerts.test.yaml

echo "✓ Alert rules are valid and all rule tests passed"
