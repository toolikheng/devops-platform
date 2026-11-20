#!/bin/bash
set -euo pipefail

# Scans the full git history of every branch for secrets with gitleaks, run as a pinned Docker image.
# --redact keeps secret values out of the output, so a finding never leaks again through logs.
# Exit code 1 means a leak was found.

echo "=== Scanning git history for secrets (gitleaks) ==="

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GITLEAKS_IMAGE="ghcr.io/gitleaks/gitleaks:v8.30.1"

# The container runs as a different user than the repo owner, so git needs safe.directory.
docker run --rm -v "$REPO_ROOT:/repo:ro" \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  "$GITLEAKS_IMAGE" git /repo --log-opts="--all" --redact --no-banner

echo "✓ No leaks found"
