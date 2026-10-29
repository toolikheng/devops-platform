#!/bin/bash
# Round-trip restore test against the in-cluster app. Proves the data comes back from the snapshot:
#   write 5 items -> back up (app stopped) -> write a sentinel item that is NOT in the backup
#   -> wipe app.db and restore the latest snapshot -> the app must serve exactly the backed-up items,
#   and the sentinel must be gone.
set -euo pipefail

NS="${NS:-devops}"
APP="deploy/devops-app"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESTORE_JOB_YAML="${RESTORE_JOB_YAML:-$SCRIPT_DIR/../restore-job.yaml}"
TS="$(date +%s)"
SENTINEL="restore-test-$TS-sentinel"

items() { kubectl -n "$NS" exec "$APP" -- curl -sf http://localhost:8000/items | jq -S 'sort_by(.id)'; }
post_item() {
  kubectl -n "$NS" exec "$APP" -- curl -sf -X POST http://localhost:8000/items \
    -H "Content-Type: application/json" -d "{\"title\":\"$1\",\"description\":\"$2\"}" > /dev/null
}
stop_app() {
  kubectl -n "$NS" scale "$APP" --replicas=0 > /dev/null
  kubectl -n "$NS" wait --for=delete pod -l app=devops-app --timeout=120s > /dev/null 2>&1 || true
}
start_app() {
  kubectl -n "$NS" scale "$APP" --replicas=1 > /dev/null
  kubectl -n "$NS" rollout status "$APP" --timeout=180s > /dev/null
}

# Whatever happens, leave the app running.
trap start_app EXIT

echo "== 1. Wait for app"
kubectl -n "$NS" rollout status "$APP" --timeout=180s

echo "== 2. Write 5 test items"
for i in 1 2 3 4 5; do post_item "restore-test-$TS-$i" "round-trip item $i"; done
EXPECTED="$(items)"
echo "items before backup: $(jq length <<< "$EXPECTED")"

echo "== 3. Stop app (so the SQLite file is quiet) and back up"
stop_app
BACKUP_JOB="restore-test-backup-$TS"
kubectl -n "$NS" create job "$BACKUP_JOB" --from=cronjob/devops-app-backup > /dev/null
kubectl -n "$NS" wait --for=condition=complete "job/$BACKUP_JOB" --timeout=300s > /dev/null
SNAPSHOT="$(kubectl -n "$NS" logs "job/$BACKUP_JOB" | sed -n 's/^snapshot \([0-9a-f]*\) saved$/\1/p')"
echo "backup snapshot: $SNAPSHOT"

echo "== 4. Write a sentinel item that is not in the backup"
start_app
post_item "$SENTINEL" "written after the backup; must be gone after the restore"
echo "items now: $(items | jq length) (including $SENTINEL)"

echo "== 5. Stop app, wipe /data/app.db, restore the latest snapshot"
stop_app
RESTORE_JOB="$(kubectl -n "$NS" create -f "$RESTORE_JOB_YAML" -o name)"
kubectl -n "$NS" wait --for=condition=complete "$RESTORE_JOB" --timeout=300s > /dev/null
RESTORE_LOG="$(kubectl -n "$NS" logs "$RESTORE_JOB")"
echo "$RESTORE_LOG"
if ! grep -q "restoring snapshot $SNAPSHOT of" <<< "$RESTORE_LOG"; then
  echo "FAIL: restore did not use the snapshot this test created ($SNAPSHOT)"
  exit 1
fi

echo "== 6. Start app and verify"
start_app
ACTUAL="$(items)"
echo "items after restore: $(jq length <<< "$ACTUAL")"

if [ "$ACTUAL" = "$EXPECTED" ]; then
  echo "PASS: the app serves exactly the $(jq length <<< "$EXPECTED") backed-up items, and $SENTINEL is gone"
else
  echo "FAIL: the data after the restore differs from the backup:"
  diff <(echo "$EXPECTED") <(echo "$ACTUAL") || true
  exit 1
fi
