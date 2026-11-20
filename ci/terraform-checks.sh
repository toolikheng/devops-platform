#!/bin/bash
set -euo pipefail

# Offline checks for every directory under terraform/ that has .tf files (stacks and modules): fmt,
# validate, terraform test (mock providers), then tflint and trivy over all of terraform/. No AWS credentials are needed: backends are
# skipped and the tests never call AWS. tflint and trivy run as pinned Docker images.

echo "=== Running Terraform checks ==="

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_ROOT="$REPO_ROOT/terraform"
TFLINT_IMAGE="ghcr.io/terraform-linters/tflint:v0.66.0"
TRIVY_IMAGE="aquasec/trivy:0.75.0"
export TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-$HOME/.terraform.d/plugin-cache}"
mkdir -p "$TF_PLUGIN_CACHE_DIR"
# Each stack is checked with its own throwaway data dir. `init -backend=false` reuses whatever backend a
# directory was initialised with, so in a stack that was really applied it would ask for AWS credentials.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "--- terraform fmt"
terraform fmt -check -recursive -diff "$TF_ROOT"

mapfile -t stacks < <(find "$TF_ROOT" -name '*.tf' -not -path '*/.terraform/*' -printf '%h\n' | sort -u)
for stack in "${stacks[@]}"; do
  echo "--- ${stack#"$REPO_ROOT"/}: init, validate, test"
  (
    cd "$stack"
    export TF_DATA_DIR="$work/${stack//\//_}"
    # readonly: the committed .terraform.lock.hcl must already cover every provider.
    terraform init -backend=false -input=false -lockfile=readonly >/dev/null
    terraform validate
    terraform test
  )
done

echo "--- tflint"
tflint() {
  docker run --rm -v "$TF_ROOT:/data" -w /data -v devops-tflint-plugins:/root/.tflint.d \
    -e GITHUB_TOKEN="${GITHUB_TOKEN:-}" "$TFLINT_IMAGE" --config=/data/.tflint.hcl "$@"
}
tflint --init
tflint --recursive

echo "--- trivy config"
docker run --rm -v "$TF_ROOT:/src:ro" -v devops-trivy-cache:/root/.cache/ "$TRIVY_IMAGE" \
  config --quiet --exit-code 1 --skip-dirs '**/.terraform' /src

echo "✓ Terraform checks passed"
