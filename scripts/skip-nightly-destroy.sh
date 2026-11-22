#!/usr/bin/env bash
# skip-nightly-destroy.sh - keep the ephemeral stack through the nightly destroy (02:00 MYT).
#
# Usage: skip-nightly-destroy.sh [UNTIL]   (make skip-nightly-destroy [UNTIL=...])
#   UNTIL: a UTC time such as 2026-11-24T00:00:00Z, at most 7 days ahead. Default: 24 hours from now.
#
# Sets the repository variable NIGHTLY_DESTROY_SKIP_UNTIL with the GitHub CLI: a GitHub setting, not an AWS
# change. nightly-destroy.yml skips while that time is in the future. The stack still bills every hour it
# runs, so the limit is 7 days, and make cloud-down remains the way to stop it.
# Cancel the skip: gh variable delete NIGHTLY_DESTROY_SKIP_UNTIL
set -euo pipefail

MAX_DAYS=7
until="${1:-$(date -u -d '+24 hours' +%Y-%m-%dT%H:%M:%SZ)}"
if ! [[ "$until" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] ||
  ! end="$(date -u -d "$until" +%s 2>/dev/null)"; then
  echo "usage: skip-nightly-destroy.sh [UNTIL]  (UNTIL: a UTC time such as 2026-11-24T00:00:00Z)" >&2
  exit 2
fi
now="$(date -u +%s)"
if [ "$end" -le "$now" ] || [ "$end" -gt $((now + MAX_DAYS * 86400)) ]; then
  echo "skip-nightly-destroy: $until must be in the future and at most $MAX_DAYS days ahead" >&2
  exit 2
fi

gh variable set NIGHTLY_DESTROY_SKIP_UNTIL --body "$until"
echo "The nightly destroy is skipped until $until (UTC). The stack keeps billing: make cloud-down when you are done."
echo "Cancel the skip: gh variable delete NIGHTLY_DESTROY_SKIP_UNTIL"
