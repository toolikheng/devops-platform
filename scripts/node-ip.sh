#!/usr/bin/env bash
# node-ip.sh - print the public IPv4 of the node in the ephemeral stack, from Terraform output.
#
# Used by make cloud-deploy and cloud-smoke to reach the UI through the ingress. Read-only: it runs
# `terraform output`, never plan or apply. Exits non-zero, printing nothing, when the stack is down or the
# output is not an IPv4 address.
set -euo pipefail

ip="$(bash "$(dirname "$0")/ephemeral-tf.sh" output -raw public_ip || true)"

octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
if ! [[ "$ip" =~ ^$octet\.$octet\.$octet\.$octet$ ]]; then
  echo "node-ip: no running node in the ephemeral stack (public_ip output: '${ip:0:40}'). Run make cloud-up first." >&2
  exit 1
fi
echo "$ip"
