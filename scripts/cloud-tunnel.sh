#!/usr/bin/env bash
# cloud-tunnel.sh - forward the node's Kubernetes API to localhost through Session Manager.
#
# Runs in the foreground: leave it open in its own terminal and press Ctrl-C to close the tunnel. Nothing
# is opened on the node or in its security group; the SSM agent carries the traffic over its outbound
# connection. kubectl then uses KUBECONFIG=~/.kube/devops-aws.yaml, which make cloud-config writes with
# server https://127.0.0.1:16443 (the k3s certificate is valid for 127.0.0.1).
set -euo pipefail

# LOCAL_PORT must match k3s_kubeconfig_local_port in ansible/roles/k3s (tests/test_ansible_aws.py checks it).
LOCAL_PORT=16443
REMOTE_PORT=6443
# The stacks fix their region in code.
REGION="ap-southeast-1"

id="$(bash "$(dirname "$0")/node-id.sh")"

echo "cloud-tunnel: localhost:$LOCAL_PORT -> $id:$REMOTE_PORT (Ctrl-C to stop)"
exec aws ssm start-session --region "$REGION" --target "$id" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "portNumber=$REMOTE_PORT,localPortNumber=$LOCAL_PORT"
