#!/usr/bin/env bash
# check-state-secrets.sh - show that the persistent Terraform state holds no secret value.
#
# For every SecureString parameter in the persistent stack's state, reads the live value from SSM and counts
# the state lines that contain it. Prints only names and counts: each value stays in a shell variable and
# reaches grep through a pipe, never as a command-line argument (visible in ps) or as output. Read-only:
# terraform state pull and ssm get-parameter. Exit 1 if any count is above 0. Run: make check-state-secrets.
set -euo pipefail

STACK="$(cd "$(dirname "$0")/../terraform/persistent" && pwd)"

state="$(cd "$STACK" && terraform state pull)"
mapfile -t names < <(jq -r '.resources[] | select(.type == "aws_ssm_parameter") | .instances[].attributes
  | select(.type == "SecureString") | .name' <<<"$state")
if [ "${#names[@]}" -eq 0 ]; then
  echo "check-state-secrets: no SecureString parameter in the persistent state, so nothing was checked." >&2
  exit 1
fi

found=0
for name in "${names[@]}"; do
  value="$(aws ssm get-parameter --region ap-southeast-1 --name "$name" --with-decryption \
    --query Parameter.Value --output text)"
  if [ -z "$value" ]; then
    echo "check-state-secrets: $name has an empty value" >&2
    exit 1
  fi
  count="$(grep -c -F -f <(printf '%s\n' "$value") <<<"$state" || true)"
  echo "$name: $count"
  if [ "$count" -ne 0 ]; then found=1; fi
done

if [ "$found" -ne 0 ]; then
  echo "check-state-secrets: a secret value is in the Terraform state" >&2
  exit 1
fi
echo "check-state-secrets: no secret value in the persistent state"
