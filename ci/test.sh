#!/bin/bash
set -euo pipefail

echo "=== Running tests ==="

cd "$(dirname "$0")/../app"

# Dependencies come from the caller: the CI job's install step, or `make venv`.
echo "Running pytest..."
python -m pytest tests/ -v --tb=short

echo "✓ All tests passed"
