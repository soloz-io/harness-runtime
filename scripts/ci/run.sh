#!/bin/bash
set -e

echo "================================================================================"
echo "Starting Harness Runtime (CLI/Subprocess Mode)"
echo "================================================================================"
echo "  DATABASE_URL:    ${DATABASE_URL:+set}"
echo "================================================================================"

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL environment variable is required"
    exit 1
fi

exec harness-runtime
