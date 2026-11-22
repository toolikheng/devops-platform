#!/bin/bash
# kubeconform.sh - validate the Kubernetes YAML on stdin against the schemas of the node's Kubernetes version
# (ci/versions.env), strictly: unknown fields fail. ExternalSecret and ClusterSecretStore are custom
# resources, so their schemas come from the CRDs catalog at a pinned commit. Extra arguments go to
# kubeconform (e.g. -skip KIND). Used by ci/k8s-checks.sh and ci/deploy-render.sh. Only Docker is needed;
# kubeconform downloads the schemas from GitHub.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBECONFORM_IMAGE="ghcr.io/yannh/kubeconform:v0.8.0"
# KUBERNETES_VERSION: the version of k3s on the AWS node.
# shellcheck source=ci/versions.env
source "$REPO_ROOT/ci/versions.env"
CRD_SCHEMAS="https://raw.githubusercontent.com/datreeio/CRDs-catalog/034f273c7ab88c24f01d828db4dfc95e86f5b3a4/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"

docker run --rm -i "$KUBECONFORM_IMAGE" -strict -summary -kubernetes-version "$KUBERNETES_VERSION" \
  -schema-location default -schema-location "$CRD_SCHEMAS" "$@" -
