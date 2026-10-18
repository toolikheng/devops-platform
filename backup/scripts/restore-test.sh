#!/bin/bash
# Round-trip restore test against the in-cluster app:
# write items -> backup CronJob -> wipe DB and restore -> verify item count matches.
set -euo pipefail

NS="${NS:-devops}"
APP="deploy/devops-app"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESTORE_JOB_YAML="$SCRIPT_DIR/../restore-job.yaml"
TS="$(date +%s)"

item_count() {
  kubectl -n "$NS" exec "$APP" -- curl -sf http://localhost:8000/items | jq 'length'
}

echo "== 1. Wait for app"
kubectl -n "$NS" rollout status "$APP" --timeout=180s

echo "== 2. Write 5 test items"
for i in 1 2 3 4 5; do
  kubectl -n "$NS" exec "$APP" -- curl -sf -X POST http://localhost:8000/items \
    -H "Content-Type: application/json" \
    -d "{\"title\":\"restore-test-$TS-$i\",\"description\":\"round-trip item $i\"}" > /dev/null
done
EXPECTED="$(item_count)"
echo "items before backup: $EXPECTED"

echo "== 3. Stop app so the SQLite file is quiet, then back up"
kubectl -n "$NS" scale "$APP" --replicas=0
kubectl -n "$NS" wait --for=delete pod -l app=devops-app --timeout=120s || true
kubectl -n "$NS" create job "restore-test-backup-$TS" --from=cronjob/devops-app-backup
kubectl -n "$NS" wait --for=condition=complete "job/restore-test-backup-$TS" --timeout=300s

echo "== 4. Wipe /data/app.db and restore latest snapshot"
RESTORE_JOB="$(kubectl -n "$NS" create -f "$RESTORE_JOB_YAML" -o name)"
kubectl -n "$NS" wait --for=condition=complete "$RESTORE_JOB" --timeout=300s
kubectl -n "$NS" logs "$RESTORE_JOB"

echo "== 5. Start app and verify"
kubectl -n "$NS" scale "$APP" --replicas=1
kubectl -n "$NS" rollout status "$APP" --timeout=180s
ACTUAL="$(item_count)"
echo "items after restore: $ACTUAL"

if [ "$ACTUAL" -eq "$EXPECTED" ]; then
  echo "PASS: restored data matches the backup"
else
  echo "FAIL: expected $EXPECTED items, found $ACTUAL"
  exit 1
fi
