#!/usr/bin/env bash
# owner-ip.sh - store this machine's public IPv4 as a /32 in the SSM parameter /devops-platform/owner-cidr.
#
# The ephemeral stack reads the parameter and allows only that address in on port 80. `make cloud-up` runs
# this first, so every session starts with the current IP; after an IP change mid-session, run
# `make cloud-up` again and only the security group rule changes. This is an AWS write (one free Standard
# parameter).
set -euo pipefail

# Must match terraform/modules/names (tests/test_names.py checks it).
PARAMETER="/devops-platform/owner-cidr"
# The stacks fix their region in code; a parameter written to any other region would never be read.
REGION="ap-southeast-1"

ip="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')"

# Four octets, each 0-255. Anything else (an error page, IPv6, nothing) is refused before AWS is called.
octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
if ! [[ "$ip" =~ ^$octet\.$octet\.$octet\.$octet$ ]]; then
  echo "owner-ip: checkip.amazonaws.com did not return an IPv4 address: '${ip:0:40}'" >&2
  exit 1
fi

aws ssm put-parameter --region "$REGION" --name "$PARAMETER" --type String --value "$ip/32" --overwrite >/dev/null
echo "owner-ip: $PARAMETER = $ip/32"
