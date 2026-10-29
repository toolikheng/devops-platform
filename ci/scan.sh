#!/bin/bash
set -euo pipefail

# Fails (exit 1) if the image has HIGH or CRITICAL vulnerabilities that have a fix available.
# Unfixed CVEs are reported by the SARIF scan in CI but don't block, since there's nothing to upgrade to.
# Trivy runs as a pinned Docker image (same version as CI), reading the image from the local Docker daemon.

echo "=== Scanning Docker image ==="

IMAGE_TAG="${1:-devops-app:latest}"
TRIVY_IMAGE="aquasec/trivy:0.75.0"

echo "Scanning $IMAGE_TAG for fixable HIGH/CRITICAL vulnerabilities..."
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v devops-trivy-cache:/root/.cache/ \
  "$TRIVY_IMAGE" image --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$IMAGE_TAG"

echo "✓ No fixable HIGH/CRITICAL vulnerabilities"
