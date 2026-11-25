#!/usr/bin/env bash
# policypal-backup - PolicyPal's backups in S3 with restic, run as root on the AWS node.
#
#   policypal-backup backup                     the Postgres dump first, then the uploads, then retention
#   policypal-backup show latest|run-<time>     a run's two snapshots: "<run> <database snapshot> <uploads snapshot>"
#   policypal-backup restore latest|run-<time>  that run's database and uploads, with the API stopped meanwhile,
#                                               then the hourly backups on
#   policypal-backup enable                     the hourly backups on, for a node with nothing to restore
#
# policypal-backup.service runs `backup` every hour once policypal-backup.timer is on. A new node leaves it off:
# retention keeps the newest snapshot of each day, so a backup of a new node's empty PolicyPal would push the last
# good one out of the repository.
#
# The dump and the uploads go together: PolicyPal deletes database rows whose upload file is missing, so both
# snapshots of a run carry the same run-<time> tag and a restore takes the pair. restic's retention decides what is
# deleted; the bucket never expires current objects. Any failure
# exits non-zero, so systemd marks the run failed (systemctl status policypal-backup), and only a successful backup
# records its time for node_exporter (policypal_backup_last_success_timestamp_seconds).
#
# Settings (no secret) are in /etc/policypal-backup.env. restic gets the repository password by running
# RESTIC_PASSWORD_COMMAND (ssm-parameter, SSM Parameter Store with the node's role): never on disk or a command line.
set -euo pipefail

usage() {
  echo "usage: policypal-backup backup | show latest|run-<time> | restore latest|run-<time> | enable" >&2
  exit 2
}

case "${1:-}" in
  backup | enable) [ "$#" -eq 1 ] || usage ;;
  show | restore) [ "$#" -eq 2 ] && [[ "$2" =~ ^(latest|run-[0-9]{8}T[0-9]{6}Z)$ ]] || usage ;;
  *) usage ;;
esac

# The service passes the settings (EnvironmentFile); a run by hand reads the same file.
settings="${POLICYPAL_BACKUP_ENV:-/etc/policypal-backup.env}"
if [ -f "$settings" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$settings"
  set +a
fi
: "${RESTIC_REPOSITORY:?set in /etc/policypal-backup.env}"
: "${RESTIC_PASSWORD_COMMAND:?set in /etc/policypal-backup.env}"
: "${RESTIC_HOST:?set in /etc/policypal-backup.env}"
: "${KUBECONFIG:?set in /etc/policypal-backup.env}"

NAMESPACE="policypal"
UPLOADS_CLAIM="policypal-uploads"
API="policypal-api"
# Retention, for each kind (dump, uploads): the same as the homelab backup.
KEEP=(--keep-daily 7 --keep-weekly 4 --keep-monthly 12)

fail() {
  echo "policypal-backup: $*" >&2
  exit 1
}

# The uploads directory on this node: local-path stores a volume in a node directory named after the volume, so it
# is looked up from the claim each time instead of being written down. It must exist.
uploads_dir() {
  local volume dir
  volume="$(kubectl -n "$NAMESPACE" get pvc "$UPLOADS_CLAIM" -o jsonpath='{.spec.volumeName}')"
  [ -n "$volume" ] || fail "no volume is bound to $UPLOADS_CLAIM"
  dir="$(kubectl get pv "$volume" -o jsonpath='{.spec.hostPath.path}{.spec.local.path}')"
  [ -n "$dir" ] && [ -d "$dir" ] || fail "the uploads directory of $volume ('$dir') is not on this node"
  echo "$dir"
}

# snapshot TAG[,TAG] - "<id> <run> <path>" of the newest snapshot carrying all the tags; fails if there is none.
snapshot() {
  restic snapshots --json --tag "$1" | python3 -c '
import json, sys
found = json.load(sys.stdin)
if not found:
    sys.exit(1)
newest = max(found, key=lambda s: s["time"])
print(newest["id"], next(t for t in newest["tags"] if t.startswith("run-")), newest["paths"][0])'
}

# pick latest|RUN - sets run, db_id, up_id and up_path: one run's two snapshots, or fails.
pick() {
  local db up
  if [ "$1" = latest ]; then
    db="$(snapshot policypal-db)" || fail "no database backup in the repository"
  else
    db="$(snapshot "policypal-db,$1")" || fail "no database backup for $1"
  fi
  read -r db_id run _ <<<"$db"
  up="$(snapshot "policypal-uploads,$run")" || fail "no uploads backup for $run: a restore needs both"
  read -r up_id _ up_path <<<"$up"
}

backup() {
  local uploads rc=0
  uploads="$(uploads_dir)"

  # Exit code 10 is restic's "repository does not exist": only then is it created. Any other error (wrong password
  # 12, lock held 11, network) stops the run: never init over a repository that exists.
  restic cat config >/dev/null || rc=$?
  if [ "$rc" -eq 10 ]; then
    restic init
  elif [ "$rc" -ne 0 ]; then
    fail "can't open the repository (restic exit $rc)"
  fi

  run="run-$(date -u +%Y%m%dT%H%M%SZ)"

  # The database first: restic runs pg_dump in the Postgres pod and stores its output. If pg_dump fails, restic
  # keeps no snapshot and exits non-zero. The user and database come from the pod's own environment.
  # shellcheck disable=SC2016 # expanded by sh in the Postgres pod, not here
  restic backup --stdin-from-command --stdin-filename policypal.dump --tag "policypal-db,$run" -- \
    kubectl -n "$NAMESPACE" exec statefulset/postgres -- sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc'

  # Then the uploads.
  restic backup --tag "policypal-uploads,$run" "$uploads"

  # Retention, only after a run that saved both: forget per kind, then prune once, then check the repository.
  restic forget --tag policypal-db --group-by host "${KEEP[@]}"
  restic forget --tag policypal-uploads --group-by host "${KEEP[@]}"
  restic prune
  restic check

  record_success
  echo "policypal-backup: done ($run)"
}

# The time of this success, for node_exporter's textfile collector. Written to a temporary name, then renamed, so
# node_exporter never reads half a file (it reads *.prom only).
record_success() {
  [ -n "${BACKUP_TEXTFILE_DIR:-}" ] || return 0
  local tmp="$BACKUP_TEXTFILE_DIR/.policypal_backup.prom.$$"
  {
    echo "# HELP policypal_backup_last_success_timestamp_seconds When the last PolicyPal backup to S3 succeeded."
    echo "# TYPE policypal_backup_last_success_timestamp_seconds gauge"
    echo "policypal_backup_last_success_timestamp_seconds $(date +%s)"
  } >"$tmp"
  chmod 0644 "$tmp"
  mv "$tmp" "$BACKUP_TEXTFILE_DIR/policypal_backup.prom"
}

show() {
  pick "$1"
  echo "$run $db_id $up_id"
}

# From here on the API is stopped. Starting it on half-restored data could delete database rows (ingestion removes
# rows whose upload file is missing), so a failure leaves it stopped for someone to look at.
stopped() {
  fail "$1. $API is left at 0 replicas: check, then kubectl -n $NAMESPACE scale deployment/$API --replicas=1"
}

restore() {
  local uploads
  pick "$1"
  uploads="$(uploads_dir)"
  echo "policypal-backup: restoring $run (database $db_id, uploads $up_id)"

  kubectl -n "$NAMESPACE" scale "deployment/$API" --replicas=0
  for _ in $(seq 90); do
    [ -z "$(kubectl -n "$NAMESPACE" get pods -l "app.kubernetes.io/name=$API" -o name)" ] && break
    sleep 2
  done
  [ -z "$(kubectl -n "$NAMESPACE" get pods -l "app.kubernetes.io/name=$API" -o name)" ] ||
    stopped "the API pod did not stop within 3 minutes"

  # A clean database, then the dump. pg_restore stops at the first error.
  # shellcheck disable=SC2016 # expanded by sh in the Postgres pod, not here
  kubectl -n "$NAMESPACE" exec statefulset/postgres -- sh -c \
    'dropdb -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"' ||
    stopped "could not recreate the database"
  # shellcheck disable=SC2016 # expanded by sh in the Postgres pod, not here
  restic dump "$db_id" /policypal.dump |
    kubectl -n "$NAMESPACE" exec -i statefulset/postgres -- sh -c \
      'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error --no-owner' ||
    stopped "the database restore failed"

  # The uploads, exactly as backed up: files the backup didn't have are deleted.
  restic restore "$up_id:$up_path" --target "$uploads" --delete || stopped "the uploads restore failed"

  kubectl -n "$NAMESPACE" scale "deployment/$API" --replicas=1
  kubectl -n "$NAMESPACE" rollout status "deployment/$API" --timeout=10m
  echo "policypal-backup: restored $run: database $db_id, uploads $up_id"
  # Only now: from here on, a backup saves the restored data.
  enable
}

enable() {
  systemctl enable --now policypal-backup.timer
  echo "policypal-backup: hourly backups on (systemctl list-timers policypal-backup.timer)"
}

"$@"
