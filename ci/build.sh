#!/bin/bash
set -euo pipefail

echo "=== Building Docker image ==="

REPO_ROOT="$(dirname "$0")/.."
APP_DIR="$REPO_ROOT/app"
IMAGE_TAG="${1:-devops-app:latest}"

echo "Building image: $IMAGE_TAG"
# The runtime stage runs apt-get upgrade; a cached copy would keep old Debian packages (app/Dockerfile).
docker build --no-cache-filter runtime -t "$IMAGE_TAG" -f "$APP_DIR/Dockerfile" "$APP_DIR"

echo "Image details:"
docker inspect "$IMAGE_TAG" --format='{{.Config.User}} | {{.Config.Labels}} | {{.Size}}' || echo "Image built"

echo "✓ Image built successfully"
