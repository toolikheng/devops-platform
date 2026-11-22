#!/bin/bash
# nightly-skip.sh - is tonight's destroy skipped? nightly-destroy.yml runs it before it touches AWS.
#
# Reads SKIP_UNTIL, the repo variable NIGHTLY_DESTROY_SKIP_UNTIL that make skip-nightly-destroy sets: a UTC
# time written as 2026-11-23T18:30:00Z. Writes skip=true to $GITHUB_OUTPUT while that time is in the future,
# skip=false otherwise. Anything unclear means destroy: a stack left running by mistake bills every hour,
# while a destroy by mistake only costs a rebuild.
set -euo pipefail

until="${SKIP_UNTIL:-}"
skip=false
if [ -z "$until" ]; then
  echo "No skip set: destroying."
elif ! [[ "$until" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] ||
  ! end="$(date -u -d "$until" +%s 2>/dev/null)"; then
  echo "::warning::NIGHTLY_DESTROY_SKIP_UNTIL is not a UTC time like 2026-11-23T18:30:00Z: destroying anyway."
elif [ "$end" -gt "$(date -u +%s)" ]; then
  skip=true
  echo "Skipping tonight's destroy: NIGHTLY_DESTROY_SKIP_UNTIL is $until (UTC)."
else
  echo "The skip ended at $until (UTC): destroying."
fi

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "skip=$skip" >>"$GITHUB_OUTPUT"
fi
