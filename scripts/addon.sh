#!/usr/bin/env bash
# addon.sh - install or render one platform add-on: a pinned Helm chart with our values.
#
#   addon.sh install NAME ENV   helm upgrade --install into kubectl's current cluster, then apply its manifests
#   addon.sh render NAME ENV    print the same chart and values rendered as YAML, then its manifests
#
# An add-on is a folder k8s/platform/NAME: chart.env (CHART, CHART_REPO, CHART_VERSION, NAMESPACE, and optional
# MANIFESTS, files applied after the chart because they need its CRDs) and one values file per environment it
# runs in (values-local.yaml for k3d, values-aws.yaml for the AWS node). The Helm release is called NAME. An
# environment without a values file is refused, so an add-on runs only where it was written for.
#
# install waits until the pods are ready, and installing again upgrades in place. render uses a pinned Helm
# image (only Docker is needed, and the network to download the chart) and the node's Kubernetes version, so
# ci/k8s-checks.sh and the tests see what the cluster gets. Used by make cloud-addons, make monitoring-up and
# ci/k8s-checks.sh.
set -euo pipefail

HELM_IMAGE="alpine/helm:3.21.4"

usage() {
  echo "usage: addon.sh install|render NAME ENV  (an add-on in k8s/platform/NAME with values-ENV.yaml)" >&2
  exit 2
}

[ "$#" -eq 3 ] || usage
action="$1" name="$2" env="$3"
case "$action" in install | render) ;; *) usage ;; esac
# Plain names only: they become paths.
[[ "$name" =~ ^[a-z0-9][a-z0-9-]*$ && "$env" =~ ^[a-z0-9]+$ ]] || usage

PLATFORM="$(cd "$(dirname "$0")/../k8s/platform" && pwd)"
dir="$PLATFORM/$name"
values="values-$env.yaml"
if [ ! -f "$dir/chart.env" ]; then
  echo "addon: no add-on $name (k8s/platform/$name/chart.env is missing)" >&2
  exit 1
fi
if [ ! -f "$dir/$values" ]; then
  echo "addon: $name has no values for $env (k8s/platform/$name/$values is missing)" >&2
  exit 1
fi

CHART="" CHART_REPO="" CHART_VERSION="" NAMESPACE="" MANIFESTS=""
# shellcheck source=/dev/null
source "$dir/chart.env"
for key in CHART CHART_REPO CHART_VERSION NAMESPACE; do
  if [ -z "${!key}" ]; then
    echo "addon: k8s/platform/$name/chart.env sets no $key" >&2
    exit 1
  fi
done
read -ra manifests <<<"$MANIFESTS"

if [ "$action" = install ]; then
  helm upgrade --install "$name" "$CHART" --repo "$CHART_REPO" --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" --create-namespace --values "$dir/$values" --wait --timeout 10m
  for manifest in "${manifests[@]}"; do
    kubectl apply -f "$dir/$manifest"
  done
else
  # KUBERNETES_VERSION: the version of k3s on the AWS node.
  # shellcheck source=ci/versions.env
  source "$(dirname "$0")/../ci/versions.env"
  docker run --rm -v "$dir:/addon:ro" "$HELM_IMAGE" \
    template "$name" "$CHART" --repo "$CHART_REPO" --version "$CHART_VERSION" \
    --namespace "$NAMESPACE" --kube-version "$KUBERNETES_VERSION" --values "/addon/$values"
  for manifest in "${manifests[@]}"; do
    echo "---"
    cat "$dir/$manifest"
  done
fi
