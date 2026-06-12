#!/bin/bash
set -euo pipefail

# ==============================================================================
# Type-check script for harness-runtime
# ==============================================================================
# Usage: ./scripts/typecheck.sh
#
# Runs ruff (lint) and ty (static type check) from within the project directory
# so module paths resolve correctly.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."

cd "$PROJECT_DIR"

echo "=== ruff: lint check ==="
ruff check .
echo "ruff: all checks passed"
echo ""

echo "=== ty: type check ==="
uv run ty core/
echo "ty: all checks passed"
echo ""

echo "✅ typecheck complete"
