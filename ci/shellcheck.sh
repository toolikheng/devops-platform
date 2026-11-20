#!/bin/bash
set -euo pipefail

# Lints every shell script in the repo with ShellCheck, run as a pinned Docker image (same version
# locally and in CI). Scripts are found by git, so ignored and local-only files are never checked.

echo "=== Running ShellCheck ==="

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHELLCHECK_IMAGE="koalaman/shellcheck:v0.11.0"

cd "$REPO_ROOT"
mapfile -t scripts < <(git ls-files --cached --others --exclude-standard -- '*.sh')
echo "Checking ${#scripts[@]} scripts"
docker run --rm -v "$REPO_ROOT:/mnt:ro" -w /mnt "$SHELLCHECK_IMAGE" "${scripts[@]}"

echo "✓ ShellCheck passed"
