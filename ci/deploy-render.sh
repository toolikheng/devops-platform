#!/bin/bash
# deploy-render.sh - the aws overlay for one PolicyPal commit, written to a file and validated: the exact YAML
# deploy.yml sends to the node (ci/deploy-over-ssm.sh), which has no Docker to render it.
#
# Usage: deploy-render.sh <policypal-commit> <out-file>
#
# The commit is PolicyPal's full hash, the image tag its CI publishes. First it asks GHCR whether that image
# exists (anonymously: the image is public). A missing tag stops here, in seconds, before anything reaches the
# node; otherwise the deploy would take the API down (Recreate) and roll back only after the 15-minute rollout
# timeout. The rollback stays as the second line of defence, for an image that exists but doesn't work.
# Anything but a clear yes or no from GHCR stops too. Then the file is rendered and checked with the same
# kubeconform run as make k8s-check, so the YAML the node applies is the YAML that passed.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="ghcr.io/toolikheng/policypal"
# Image index or single manifest, OCI or Docker format: whichever PolicyPal's CI publishes.
ACCEPT="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json,"
ACCEPT+=" application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"

tag="${1:-}"
out="${2:-}"
if ! [[ "$tag" =~ ^[0-9a-f]{40}$ ]] || [ -z "$out" ]; then
  echo "usage: deploy-render.sh <policypal-commit: 40 lowercase hex characters> <out-file>" >&2
  exit 2
fi

registry="${IMAGE%%/*}"
repository="${IMAGE#*/}"
status=""
if token="$(curl -fsS --max-time 20 "https://$registry/token?scope=repository:$repository:pull" | jq -er .token)"; then
  status="$(curl -sS --max-time 20 -I -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $token" \
    -H "Accept: $ACCEPT" "https://$registry/v2/$repository/manifests/$tag")" || status=""
fi
case "$status" in
  200) echo "deploy-render: $IMAGE:$tag exists on GHCR" ;;
  404)
    echo "deploy-render: $IMAGE:$tag is not on GHCR: nothing was rendered or sent to the node." \
      "Is it PolicyPal's full commit hash, and did its CI publish the image?" >&2
    exit 1
    ;;
  *)
    echo "deploy-render: could not check $IMAGE:$tag on GHCR (HTTP status '${status:-none}'): stopping" >&2
    exit 1
    ;;
esac

bash "$REPO_ROOT/scripts/render-k8s.sh" aws "$tag" >"$out"
bash "$REPO_ROOT/ci/kubeconform.sh" <"$out"
echo "deploy-render: $out ($(wc -c <"$out") bytes) is the aws overlay for PolicyPal $tag, validated"
