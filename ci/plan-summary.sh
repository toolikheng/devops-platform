#!/bin/bash
# plan-summary.sh - what a saved Terraform plan changes, as "action address" lines, and a hash of them.
#
# Usage: plan-summary.sh PLANFILE                print the changes
#        plan-summary.sh --hash PLANFILE         print the SHA-256 of that list
#        plan-summary.sh --check SHA256 PLANFILE print the changes; exit 1 unless their hash is SHA256
#
# Addresses and actions only, never attribute values: the repo is public, so are its Actions logs and PR
# comments. terraform.yml shows the list and its hash before the owner approves an apply; the apply job then
# plans again and applies its saved plan only if --check matches, so nothing the owner did not see is
# applied. Data source reads and no-op entries are left out; delete-and-create is "replace". Run it in the
# stack's directory: terraform show reads the plan with that stack's providers.
set -euo pipefail

usage() {
  echo "usage: plan-summary.sh [--hash | --check SHA256] PLANFILE" >&2
  exit 2
}

mode="list"
expected=""
case "${1:-}" in
  --hash) mode="hash"; shift ;;
  --check) mode="check"; expected="${2:-}"; shift 2 || usage ;;
esac
if [ "$mode" = check ] && ! [[ "$expected" =~ ^[0-9a-f]{64}$ ]]; then usage; fi
if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then usage; fi

json="$(terraform show -json "$1")"
lines="$(jq -r '
  [(.resource_changes // [])[]
    | {address, action: (.change.actions | sort | join(","))}
    | select(.action != "no-op" and .action != "read")
    | .action |= (if . == "create,delete" then "replace" else . end)]
  | sort_by(.address)[]
  | "\(.action)\t\(.address)"' <<<"$json" | awk -F '\t' '{ printf "%-7s %s\n", $1, $2 }')"
summary="${lines:+$lines$'\n'}"
hash="$(printf '%s' "$summary" | sha256sum | cut -d ' ' -f 1)"

case "$mode" in
  list) printf '%s' "$summary" ;;
  hash) echo "$hash" ;;
  check)
    printf '%s' "$summary"
    if [ "$hash" != "$expected" ]; then
      echo "plan-summary: the changes have changed since they were approved (hash $hash, approved $expected)." >&2
      echo "Nothing was applied. Run the workflow again and review the new plan." >&2
      exit 1
    fi
    ;;
esac
