#!/usr/bin/env bash
# node-id.sh - print the instance ID of the node in the ephemeral stack, from Terraform output.
#
# Used by aws-inventory.sh (make cloud-config) and cloud-tunnel.sh (make cloud-tunnel). Read-only: it runs
# `terraform output`, never plan or apply. Exits non-zero, printing nothing, when the stack is down or the
# output is not an instance ID, so callers never hand Session Manager a wrong target.
set -euo pipefail

id="$(bash "$(dirname "$0")/ephemeral-tf.sh" output -raw instance_id || true)"

# Instance IDs are "i-" and 8 (older) or 17 hex characters.
if ! [[ "$id" =~ ^i-([0-9a-f]{8}|[0-9a-f]{17})$ ]]; then
  echo "node-id: no running node in the ephemeral stack (instance_id output: '${id:0:40}'). Run make cloud-up first." >&2
  exit 1
fi
echo "$id"
