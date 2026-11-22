#!/bin/bash
# ephemeral-stack.sh - every Terraform run of the GitHub workflows on the ephemeral stack (terraform/envs/aws).
#
# Usage: ephemeral-stack.sh plan          read-only plan: pull requests, and the plan shown before a manual run
#        ephemeral-stack.sh apply SHA256  plan again (locked) and apply that saved plan only if its changes
#                                         hash to SHA256, the hash the owner approved
#        ephemeral-stack.sh nightly       plan the destroy and apply it (nightly-destroy.yml)
#
# From the environment: ACTION (apply or destroy; empty on pull requests means apply), INSTANCE_TYPE and
# USE_SPOT (empty: the stack's defaults), TF_VAR_ssh_public_key. Plans go to a temporary file, never left in
# the workspace, and are never printed: Terraform's own output includes values (the owner's IP), and the
# repo's logs are public. What reaches the log, the run summary ($GITHUB_STEP_SUMMARY) and, for plan, the job
# outputs ($GITHUB_OUTPUT) is ci/plan-summary.sh's list of addresses and actions and its hash. A saved plan
# applies as it is: nothing is auto-approved.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FENCE=$'\x60\x60\x60' # three backticks: a Markdown code block

usage() {
  echo "usage: ephemeral-stack.sh plan | apply SHA256 | nightly   (ACTION=apply|destroy)" >&2
  exit 2
}

case "${1:-}:$#" in
  plan:1 | nightly:1) ;;
  apply:2) [[ "$2" =~ ^[0-9a-f]{64}$ ]] || usage ;;
  *) usage ;;
esac
case "${ACTION:-apply}" in
  apply) mode=() ;;
  destroy) mode=(-destroy) ;;
  *) usage ;;
esac
inputs=()
if [ -n "${INSTANCE_TYPE:-}" ]; then inputs+=(-var "instance_type=$INSTANCE_TYPE"); fi
if [ -n "${USE_SPOT:-}" ]; then inputs+=(-var "use_spot=$USE_SPOT"); fi

changes() { bash "$REPO_ROOT/ci/plan-summary.sh" "$@"; }
# To the log, and to the run's summary page when there is one (GitHub's API can't read job summaries).
show() { if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then tee -a "$GITHUB_STEP_SUMMARY"; else cat; fi; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
plan="$work/tfplan"
cd "$REPO_ROOT/terraform/envs/aws"
terraform init -input=false >/dev/null

case "$1" in
  plan)
    # The plan role may read the state but not write its lockfile.
    terraform plan "${mode[@]}" "${inputs[@]}" -lock=false -input=false -out="$plan" >/dev/null
    list="$(changes "$plan")"
    hash="$(changes --hash "$plan")"
    {
      echo "### Terraform plan: ephemeral stack"
      echo
      if [ -n "$list" ]; then printf '%s\n%s\n%s\n' "$FENCE" "$list" "$FENCE"; else echo "No changes."; fi
      echo
      echo "Hash of these changes: \`$hash\`"
    } | show
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
      printf 'hash=%s\nsummary<<SUMMARY_END\n%s\nSUMMARY_END\n' "$hash" "$list" >>"$GITHUB_OUTPUT"
    fi
    ;;
  apply)
    terraform plan "${mode[@]}" "${inputs[@]}" -lock-timeout=5m -input=false -out="$plan" >/dev/null
    changes --check "$2" "$plan"
    terraform apply -input=false "$plan" >/dev/null
    echo "Applied. Next for PolicyPal: make cloud-config and make cloud-addons from the laptop, then deploy.yml." | show
    ;;
  nightly)
    # Always a destroy with the stack's defaults; waits for a laptop run holding the lock instead of failing.
    terraform plan -destroy -lock-timeout=10m -input=false -out="$plan" >/dev/null
    list="$(changes "$plan")"
    { echo "### Nightly destroy"; printf '%s\n%s\n%s\n' "$FENCE" "$list" "$FENCE"; } | show
    terraform apply -input=false "$plan" >/dev/null
    ;;
esac
