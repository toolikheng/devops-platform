#!/bin/bash
set -euo pipefail

echo "=== Building Docker image ==="

REPO_ROOT="$(dirname "$0")/.."
APP_DIR="$REPO_ROOT/app"
IMAGE_TAG="${1:-devops-app:latest}"

echo "Building image: $IMAGE_TAG"
docker build -t "$IMAGE_TAG" -f "$APP_DIR/Dockerfile" "$APP_DIR"

echo "Image details:"
docker inspect "$IMAGE_TAG" --format='{{.Config.User}} | {{.Config.Labels}} | {{.Size}}' || echo "Image built"

echo "✓ Image built successfully"
