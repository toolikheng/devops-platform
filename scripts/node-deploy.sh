#!/usr/bin/env bash
# node-deploy.sh - deploy PolicyPal on the AWS node itself.
#
# Usage: node-deploy.sh <manifests.yaml>
#
# deploy.yml runs it on the node through SSM Run Command (as root), from a checkout of the same commit,
# with the manifests CI rendered and validated (ci/deploy-render.sh): the node has no Docker to render them.
# kubectl uses k3s's admin kubeconfig, which never leaves the node. deploy-policypal.sh then applies, waits,
# smoke-tests and rolls back on failure, as on the laptop. The smoke test reaches the UI through the ingress
# at the node's private address: the security group lets only the owner's IP in on port 80, and traffic
# from the node to its own address never leaves the machine. The address comes from the instance metadata
# service, IMDSv2: a session token first, then the address with that token.
set -euo pipefail

IMDS="http://169.254.169.254/latest"
here="$(dirname "$0")"

manifests="${1:-}"
if [ -z "$manifests" ] || [ ! -f "$manifests" ]; then
  echo "usage: node-deploy.sh <manifests.yaml>" >&2
  exit 2
fi

export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
if ! token="$(curl -fsS --max-time 5 -X PUT -H "X-aws-ec2-metadata-token-ttl-seconds: 60" "$IMDS/api/token")" ||
  ! ip="$(curl -fsS --max-time 5 -H "X-aws-ec2-metadata-token: $token" "$IMDS/meta-data/local-ipv4")" ||
  ! [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  echo "node-deploy: cannot read the node's private address from the instance metadata service" >&2
  exit 1
fi

DEPLOY_MANIFESTS="$manifests" exec bash "$here/deploy-policypal.sh" aws "http://$ip"
