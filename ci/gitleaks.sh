#!/bin/bash
set -euo pipefail

# Scans for secrets with gitleaks, run as a pinned Docker image, using the repo's .gitleaks.toml (default
# rules plus a rule for plain *_PASSWORD= assignments) and .gitleaksignore (known findings, with reasons).
#   ci/gitleaks.sh           the full git history of every branch (CI and the weekly Secret scan workflow)
#   ci/gitleaks.sh --staged  only the staged changes (the pre-commit hook in .pre-commit-config.yaml)
# --verbose shows each finding (file, line, rule, commit) and --redact replaces the secret in it, so a
# finding can be found and fixed but never leaks again through logs. Exit code 1 means a leak was found.

GITLEAKS_IMAGE="ghcr.io/gitleaks/gitleaks:v8.30.1"

case "${1:-}" in
  "") echo "=== Scanning git history for secrets (gitleaks) ==="; scan=(--log-opts="--all") ;;
  --staged) echo "=== Scanning staged changes for secrets (gitleaks) ==="; scan=(--pre-commit --staged) ;;
  *) echo "usage: ci/gitleaks.sh [--staged]" >&2; exit 2 ;;
esac

# The repository the command runs in: the hook and CI both start at its top level.
REPO_ROOT="$(git rev-parse --show-toplevel)"

# The container runs as a different user than the repo owner, so git needs safe.directory.
docker run --rm -v "$REPO_ROOT:/repo:ro" \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
  "$GITLEAKS_IMAGE" git /repo "${scan[@]}" --redact --verbose --no-banner

echo "✓ No leaks found"
