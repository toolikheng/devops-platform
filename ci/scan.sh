#!/bin/bash
set -euo pipefail

# Fails (exit 1) if the image has HIGH or CRITICAL vulnerabilities that have a fix available.
# Unfixed CVEs are reported by the SARIF scan in CI but don't block, since there's nothing to upgrade to.

echo "=== Scanning Docker image ==="

IMAGE_TAG="${1:-devops-app:latest}"

if ! command -v trivy &> /dev/null; then
  echo "trivy is not installed. Run 'make install' or see https://trivy.dev/latest/getting-started/installation/" >&2
  exit 1
fi

echo "Scanning $IMAGE_TAG for fixable HIGH/CRITICAL vulnerabilities..."
trivy image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$IMAGE_TAG"

echo "✓ No fixable HIGH/CRITICAL vulnerabilities"
