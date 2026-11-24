#!/usr/bin/env bash
# policypal-backup - back up PolicyPal to S3 with restic: the Postgres dump first, then the uploads.
#
# Run as root on the AWS node by policypal-backup.service (hourly timer), which sets the environment below.
# The dump and the uploads go together: PolicyPal deletes database rows whose upload file is missing, so a
# restore needs both from the same run (they share a run-<time> tag). restic's retention decides what is
# deleted; the bucket never expires current objects. Any failure exits non-zero, so systemd marks the run failed:
#   systemctl status policypal-backup     journalctl -u policypal-backup
#
# The repository password never touches disk or a command line: restic runs RESTIC_PASSWORD_COMMAND
# (ssm-parameter, reading SSM Parameter Store with the node's role) and reads the password from its output.
set -euo pipefail

: "${RESTIC_REPOSITORY:?set by policypal-backup.service}"
: "${RESTIC_PASSWORD_COMMAND:?set by policypal-backup.service}"
: "${RESTIC_HOST:?set by policypal-backup.service}"
: "${KUBECONFIG:?set by policypal-backup.service}"

NAMESPACE="policypal"
UPLOADS_CLAIM="policypal-uploads"
# Retention, for each kind (dump, uploads): the same as the homelab backup.
KEEP=(--keep-daily 7 --keep-weekly 4 --keep-monthly 12)

fail() {
  echo "policypal-backup: $*" >&2
  exit 1
}

# 1. The uploads directory on this node: local-path stores a volume in a node directory named after the volume,
# so it is looked up from the claim each time instead of being written down. Checked before anything is backed up.
volume="$(kubectl -n "$NAMESPACE" get pvc "$UPLOADS_CLAIM" -o jsonpath='{.spec.volumeName}')"
[ -n "$volume" ] || fail "no volume is bound to $UPLOADS_CLAIM"
uploads="$(kubectl get pv "$volume" -o jsonpath='{.spec.hostPath.path}{.spec.local.path}')"
[ -n "$uploads" ] && [ -d "$uploads" ] || fail "the uploads directory of $volume ('$uploads') is not on this node"

# 2. The repository. Exit code 10 is restic's "repository does not exist": only then is it created. Any other
# error (wrong password 12, lock held 11, network) stops the run: never init over a repository that exists.
rc=0
restic cat config >/dev/null || rc=$?
if [ "$rc" -eq 10 ]; then
  restic init
elif [ "$rc" -ne 0 ]; then
  fail "can't open the repository (restic exit $rc)"
fi

run="run-$(date -u +%Y%m%dT%H%M%SZ)"

# 3. The database first: restic runs pg_dump in the Postgres pod and stores its output. If pg_dump fails,
# restic keeps no snapshot and exits non-zero. The user and database come from the pod's own environment.
# shellcheck disable=SC2016 # expanded by sh in the Postgres pod, not here
restic backup --stdin-from-command --stdin-filename policypal.dump --tag "policypal-db,$run" -- \
  kubectl -n "$NAMESPACE" exec statefulset/postgres -- sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc'

# 4. Then the uploads.
restic backup --tag "policypal-uploads,$run" "$uploads"

# 5. Retention, only after a run that saved both: forget per kind, then prune once, then check the repository.
restic forget --tag policypal-db --group-by host "${KEEP[@]}"
restic forget --tag policypal-uploads --group-by host "${KEEP[@]}"
restic prune
restic check

echo "policypal-backup: done ($run)"
