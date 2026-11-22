#!/bin/bash
# deploy-over-ssm.sh - deploy PolicyPal on the AWS node through SSM Run Command, from GitHub Actions.
#
# Usage: deploy-over-ssm.sh <manifests.yaml>   (GITHUB_REPOSITORY and GITHUB_SHA are set by Actions)
#
# Finds the one running node tagged as the ephemeral stack and sends it a short shell script: fetch this
# repository at this commit, unpack the manifests (gzip + base64, inside the command), and run
# scripts/node-deploy.sh with them. The deploy, the smoke test and, if either fails, the rollback all run on
# the node with its own kubeconfig: nothing connects to the node (its SSM agent fetches the command) and no
# cluster credential leaves it. Then waits for the result and prints the node's output.
#
# The manifests are the bytes ci/deploy-render.sh validated. No AWS quota is documented for Run Command
# parameters, so a payload over MAX_PAYLOAD (half the 64 KB document size limit) is refused here, before any
# AWS call; docs/decisions/021-deploy-through-ssm-run-command.md describes the fallback.
set -euo pipefail

REGION="ap-southeast-1"
MAX_PAYLOAD=32768
EXECUTION_TIMEOUT=1800 # on the node; the deploy itself waits up to 15 minutes for a rollout
POLL_SECONDS="${POLL_SECONDS:-10}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-2400}"

manifests="${1:-}"
repo="${GITHUB_REPOSITORY:-}"
sha="${GITHUB_SHA:-}"
# Both end up in the node's shell script, so only their exact shapes are accepted.
if [ -z "$manifests" ] || [ ! -f "$manifests" ] || ! [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
  ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: GITHUB_REPOSITORY=<owner/repo> GITHUB_SHA=<commit> deploy-over-ssm.sh <manifests.yaml>" >&2
  exit 2
fi

payload="$(gzip -9n <"$manifests" | base64 -w0)"
if [ "${#payload}" -gt "$MAX_PAYLOAD" ]; then
  echo "deploy-over-ssm: the manifests are too large for one Run Command parameter" \
    "(${#payload} bytes encoded, ceiling $MAX_PAYLOAD). See docs/decisions/021-deploy-through-ssm-run-command.md." >&2
  exit 2
fi

read -r -a nodes <<<"$(aws ec2 describe-instances --region "$REGION" \
  --filters Name=tag:Project,Values=devops-platform Name=tag:Stack,Values=ephemeral \
  Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
if [ "${#nodes[@]}" -ne 1 ]; then
  echo "deploy-over-ssm: expected one running node tagged Project=devops-platform, Stack=ephemeral;" \
    "found ${#nodes[@]}. Create it with terraform.yml or make cloud-rebuild first." >&2
  exit 1
fi
node="${nodes[0]}"

# Plain sh: AWS-RunShellScript runs the commands with /bin/sh. HOME: the agent's environment has none, and
# git needs one. Single quotes on purpose: $HOME and $work expand on the node, not here.
# shellcheck disable=SC2016
commands=(
  'set -eu'
  'export HOME="${HOME:-/root}"'
  'work="$(mktemp -d)"'
  "trap 'rm -rf \"\$work\"' EXIT"
  "git clone --quiet --filter=blob:none --no-checkout https://github.com/$repo.git \"\$work/repo\""
  "git -C \"\$work/repo\" checkout --quiet $sha"
  "printf '%s' '$payload' | base64 -d | gunzip >\"\$work/policypal.yaml\""
  'bash "$work/repo/scripts/node-deploy.sh" "$work/policypal.yaml"'
)
parameters="$(jq -cn --arg timeout "$EXECUTION_TIMEOUT" \
  '{commands: $ARGS.positional, executionTimeout: [$timeout]}' --args "${commands[@]}")"

command_id="$(aws ssm send-command --region "$REGION" --instance-ids "$node" \
  --document-name AWS-RunShellScript --comment "deploy PolicyPal manifests, devops-platform ${sha:0:12}" \
  --timeout-seconds 300 --parameters "$parameters" --query Command.CommandId --output text)"
echo "deploy-over-ssm: sent command $command_id to $node; waiting for the deploy on the node"

err="$(mktemp)"
trap 'rm -f "$err"' EXIT
start=$SECONDS
status=""
while :; do
  if invocation="$(aws ssm get-command-invocation --region "$REGION" --command-id "$command_id" \
    --instance-id "$node" --output json 2>"$err")"; then
    status="$(jq -r .Status <<<"$invocation")"
    case "$status" in
      Success | Failed | TimedOut | Cancelled) break ;;
    esac
  elif ! grep -q InvocationDoesNotExist "$err"; then
    # Right after SendCommand the invocation may not exist for a moment; anything else is an error.
    cat "$err" >&2
    exit 1
  fi
  if [ $((SECONDS - start)) -ge "$MAX_WAIT_SECONDS" ]; then
    echo "deploy-over-ssm: gave up after ${MAX_WAIT_SECONDS}s (status ${status:-unknown}). Check it with:" \
      "aws ssm get-command-invocation --command-id $command_id --instance-id $node" >&2
    exit 1
  fi
  sleep "$POLL_SECONDS"
done

echo "--- output on the node ($status)"
jq -r .StandardOutputContent <<<"$invocation"
node_errors="$(jq -r .StandardErrorContent <<<"$invocation")"
if [ -n "$node_errors" ]; then
  echo "--- errors on the node" >&2
  printf '%s\n' "$node_errors" >&2
fi
if [ "$status" != Success ]; then
  echo "deploy-over-ssm: the deploy on the node ended with $status" >&2
  exit 1
fi
