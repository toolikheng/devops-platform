#!/usr/bin/env bash
# restore-test.sh before|after [STATE_FILE] - prove a PolicyPal backup survives a rebuild of the AWS node.
#
#   before   on the running node: ask a unique question and upload a unique policy (the sentinels), back up now,
#            and record the run, its two snapshots, the node's instance ID and the upload's sha256 in STATE_FILE
#   then     make cloud-down, make cloud-rebuild, make cloud-addons, make cloud-deploy: a new node, new empty data
#   after    on the new node: the sentinels must be absent; restore the recorded run; the restore must report the
#            recorded snapshots; the question and the policy must be back, and the policy file's bytes the same
#
# Node commands run as root through SSM Run Command (policypal-backup on the node, ADR 025); the API is reached
# through kubectl port-forward, so make cloud-tunnel must be running. The admin token is read from SSM Parameter
# Store and reaches curl on stdin only. STATE_FILE (default ~/.cache/devops-platform/restore-test.env, mode 600)
# holds IDs and hashes, no secret. Run by make cloud-restore-test-before and make cloud-restore-test-after.
set -euo pipefail

usage() {
  echo "usage: restore-test.sh before|after [STATE_FILE]" >&2
  exit 2
}
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
case "$1" in before | after) ;; *) usage ;; esac
PHASE="$1"
STATE="${2:-$HOME/.cache/devops-platform/restore-test.env}"

SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
# The record holds snapshot and instance IDs. Kept out of the working tree, it can't be committed by accident
# (.gitignore also ignores restore-test*.env, as a second guard).
REPO_ROOT="$(cd "$SCRIPTS/.." && pwd -P)"
case "$(realpath -m -- "$STATE")/" in
  "$REPO_ROOT"/*)
    echo "restore-test.sh: keep STATE_FILE outside the repository ($REPO_ROOT), e.g. the default" \
      "$HOME/.cache/devops-platform/restore-test.env" >&2
    exit 2
    ;;
esac
# The stacks fix their region in code.
REGION="ap-southeast-1"
# What the before phase records (KEY=value lines) and the after phase reads back: no secret, only IDs and the
# sentinels.
STATE_KEYS=(RUN DB_SNAPSHOT UPLOADS_SNAPSHOT INSTANCE SLUG QUESTION SHA256)
NAMESPACE="policypal"

fail() {
  echo "restore-test: FAIL $*" >&2
  exit 1
}

work="$(mktemp -d)"
forward=""
cleanup() {
  [ -z "$forward" ] || kill "$forward" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

# A port-forward to the API (as scripts/smoke-test.sh does). Started again after a restore, which replaces the pod.
connect() {
  [ -z "$forward" ] || kill "$forward" 2>/dev/null || true
  kubectl -n "$NAMESPACE" port-forward svc/policypal-api :8000 >"$work/forward.log" 2>&1 &
  forward=$!
  local port="" exited
  for _ in $(seq 20); do
    # Checked before reading the log, so a kubectl that has exited has also finished writing it.
    exited=""
    kill -0 "$forward" 2>/dev/null || exited=yes
    port="$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9]*\) .*/\1/p' "$work/forward.log")"
    if [ -n "$port" ] || [ -n "$exited" ]; then break; fi
    sleep 0.5
  done
  [ -n "$port" ] || fail "kubectl port-forward to the API did not start: $(head -c 300 "$work/forward.log")"
  api="http://127.0.0.1:$port"
}

# admin ARGS... - curl with the admin token as a header read from stdin (curl --config -): never an argument.
admin() {
  printf 'header = "X-Admin-Token: %s"\n' "$token" | curl -fsS --max-time 60 --config - "$@"
}

# on_node COMMAND - run COMMAND as root on the node (SSM Run Command, scripts/ssm-run.sh): prints its output, and
# fails if it failed there or AWS refused (ssm-run.sh shows why).
on_node() {
  bash "$SCRIPTS/ssm-run.sh" "$(bash "$SCRIPTS/node-id.sh")" "$1" || fail "on the node: '$1'"
}

# The sentinels as the API shows them: is the policy listed, and is the question in the history?
has_policy() { curl -fsS --max-time 30 "$api/documents" | jq -e --arg s "$SLUG" 'any(.[]; .slug == $s)' >/dev/null; }
has_question() {
  admin "$api/analytics?days=1" |
    jq -e --arg q "$QUESTION" 'any((.top_questions + .unanswered_questions)[]; .question == $q)' >/dev/null
}

token="$(aws ssm get-parameter --region "$REGION" --name /policypal/aws/admin-token --with-decryption \
  --query Parameter.Value --output text)"
connect

if [ "$PHASE" = before ]; then
  # Lower case: PolicyPal stores slugs and question history in lower case.
  SLUG="restore-test-$(date -u +%Y%m%dt%H%M%Sz)"
  QUESTION="$SLUG: is there a policy on travelling to the moon?"

  echo "== the sentinels: a question and a policy that exist only on this node"
  curl -fsS --max-time 60 -H "Content-Type: application/json" -d "$(jq -cn --arg q "$QUESTION" '{question: $q}')" \
    "$api/ask" >/dev/null
  printf '# Restore test %s\n\nThis policy exists only to prove that a backup can be restored on a new node.\n' \
    "$SLUG" >"$work/$SLUG.md"
  admin -F "file=@$work/$SLUG.md;filename=$SLUG.md" "$api/documents" | jq -e '.status == "created"' >/dev/null ||
    fail "the sentinel policy was not created"
  SHA256="$(sha256sum "$work/$SLUG.md" | cut -c1-64)"

  echo "== a backup now"
  on_node "systemctl start policypal-backup"
  read -r RUN DB_SNAPSHOT UPLOADS_SNAPSHOT <<<"$(on_node "/usr/local/sbin/policypal-backup show latest")"
  INSTANCE="$(bash "$SCRIPTS/node-id.sh")"

  mkdir -p "$(dirname "$STATE")"
  (umask 077 && for key in "${STATE_KEYS[@]}"; do printf '%s=%s\n' "$key" "${!key}"; done >"$STATE")
  echo "restore-test: recorded $RUN in $STATE. Next: make cloud-down, cloud-rebuild, cloud-addons, cloud-deploy,"
  echo "then make cloud-restore-test-after."
  exit 0
fi

[ -f "$STATE" ] || fail "no $STATE: run the before phase first"
while IFS='=' read -r key value; do
  case " ${STATE_KEYS[*]} " in *" $key "*) printf -v "$key" '%s' "$value" ;; esac
done <"$STATE"
for key in "${STATE_KEYS[@]}"; do
  [ -n "${!key:-}" ] || fail "$STATE has no $key: run the before phase again"
done

echo "== a new node, without the sentinels"
[ "$(bash "$SCRIPTS/node-id.sh")" != "$INSTANCE" ] ||
  fail "this is the same node the backup was made on ($INSTANCE): rebuild it first"
! has_policy || fail "the policy $SLUG is already there: nothing would prove the restore"
! has_question || fail "the question of $SLUG is already there: nothing would prove the restore"

echo "== restore $RUN"
restored="$(on_node "/usr/local/sbin/policypal-backup restore $RUN")"
echo "$restored" | tail -3
grep -q "database $DB_SNAPSHOT, uploads $UPLOADS_SNAPSHOT" <<<"$restored" ||
  fail "the restore did not report the recorded snapshots (database $DB_SNAPSHOT, uploads $UPLOADS_SNAPSHOT)"
# Where the node script restored the uploads ("... into <dir>"): it found the directory from the volume.
uploads="$(sed -n 's/^.* into \(\/[^ ]*\)$/\1/p' <<<"$restored" | tail -1)"
[ -n "$uploads" ] || fail "the restore did not say which directory it restored the uploads into"

echo "== the sentinels are back"
connect
has_policy || fail "the policy $SLUG did not come back"
has_question || fail "the question of $SLUG did not come back"
[ "$(on_node "sha256sum $uploads/$SLUG.md" | cut -c1-64)" = "$SHA256" ] ||
  fail "the restored policy file's sha256 is not the uploaded file's"
echo "restore-test: PASS: $RUN restored on a new node (snapshots, question, policy and its bytes)"
