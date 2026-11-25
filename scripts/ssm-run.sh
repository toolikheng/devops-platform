#!/usr/bin/env bash
# ssm-run.sh INSTANCE COMMAND... - run COMMAND lines as root on the AWS node through SSM Run Command, and wait.
#
# The lines are one /bin/sh script (AWS-RunShellScript): nothing connects to the node, its SSM agent fetches the
# command. Prints the node's output on stdout (callers parse it) and its errors on stderr, and exits 0 only if the
# command succeeded on the node. Used by ci/deploy-over-ssm.sh (deploy.yml) and scripts/restore-test.sh.
#
# Waiting: right after SendCommand, GetCommandInvocation may answer InvocationDoesNotExist for a moment, so that is
# waited out. Any other error (an expired login, AccessDenied) stops at once with AWS's message. A command still
# running after MAX_WAIT_SECONDS is given up on here (it may still finish on the node).
#
# Environment: POLL_SECONDS (default 5), MAX_WAIT_SECONDS (1200), SSM_COMMENT (shown in the console, 100
# characters at most), SSM_EXECUTION_TIMEOUT (seconds the node may spend; SSM's default is 3600).
set -euo pipefail

REGION="ap-southeast-1"
POLL_SECONDS="${POLL_SECONDS:-5}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-1200}"

if [ "$#" -lt 2 ]; then
  echo "usage: ssm-run.sh INSTANCE COMMAND..." >&2
  exit 2
fi
instance="$1"
shift

parameters="$(jq -cn --arg timeout "${SSM_EXECUTION_TIMEOUT:-}" \
  '{commands: $ARGS.positional} + (if $timeout == "" then {} else {executionTimeout: [$timeout]} end)' --args "$@")"
comment=()
[ -z "${SSM_COMMENT:-}" ] || comment=(--comment "$SSM_COMMENT")
command_id="$(aws ssm send-command --region "$REGION" --instance-ids "$instance" \
  --document-name AWS-RunShellScript "${comment[@]}" --timeout-seconds 300 --parameters "$parameters" \
  --query Command.CommandId --output text)"

err="$(mktemp)"
trap 'rm -f "$err"' EXIT
start=$SECONDS
status=""
while :; do
  if invocation="$(aws ssm get-command-invocation --region "$REGION" --command-id "$command_id" \
    --instance-id "$instance" --output json 2>"$err")"; then
    status="$(jq -r .Status <<<"$invocation")"
    case "$status" in
      Success | Failed | TimedOut | Cancelled) break ;;
    esac
  elif ! grep -q InvocationDoesNotExist "$err"; then
    cat "$err" >&2
    exit 1
  fi
  if [ $((SECONDS - start)) -ge "$MAX_WAIT_SECONDS" ]; then
    echo "ssm-run: gave up after ${MAX_WAIT_SECONDS}s (status ${status:-unknown}). Check it with:" \
      "aws ssm get-command-invocation --command-id $command_id --instance-id $instance" >&2
    exit 1
  fi
  sleep "$POLL_SECONDS"
done

# -j: exactly the node's text, no newline added.
jq -j '.StandardOutputContent // ""' <<<"$invocation"
jq -j '.StandardErrorContent // ""' <<<"$invocation" >&2
if [ "$status" != Success ]; then
  echo "ssm-run: the command ended $status on $instance" >&2
  exit 1
fi
