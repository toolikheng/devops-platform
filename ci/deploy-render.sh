#!/bin/bash
# deploy-render.sh - the aws overlay for one PolicyPal commit, written to a file and validated: the exact YAML
# deploy.yml sends to the node (ci/deploy-over-ssm.sh), which has no Docker to render it.
#
# Usage: deploy-render.sh <policypal-commit> <out-file>
#
# The commit is PolicyPal's full hash, the image tag its CI publishes. The file is checked with the same
# kubeconform run as make k8s-check, so the YAML the node applies is the YAML that passed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tag="${1:-}"
out="${2:-}"
if ! [[ "$tag" =~ ^[0-9a-f]{40}$ ]] || [ -z "$out" ]; then
  echo "usage: deploy-render.sh <policypal-commit: 40 lowercase hex characters> <out-file>" >&2
  exit 2
fi

bash "$REPO_ROOT/scripts/render-k8s.sh" aws "$tag" >"$out"
bash "$REPO_ROOT/ci/kubeconform.sh" <"$out"
echo "deploy-render: $out ($(wc -c <"$out") bytes) is the aws overlay for PolicyPal $tag, validated"
