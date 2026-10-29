#!/bin/bash
set -euo pipefail

echo "=== Running linters ==="

cd "$(dirname "$0")/../app"

echo "Checking with ruff..."
python -m ruff check .

echo "Type checking with mypy..."
python -m mypy src/app/ --ignore-missing-imports

echo "✓ All linters passed"
