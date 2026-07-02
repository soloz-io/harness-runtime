#!/bin/bash
set -e

echo "================================================================================"
echo "Starting Harness Runtime (HTTP Server Mode)"
echo "================================================================================"
echo "  DATABASE_URL:    ${DATABASE_URL:+set}"
echo "  PORT:            ${PORT:-3000}"
echo "================================================================================"

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL environment variable is required"
    exit 1
fi

# Source proxy config from Agent Vault sidecar (shared volume)
if [ -f /shared/proxy.env ]; then
    set -a
    . /shared/proxy.env
    set +a
fi

exec harness-runtime
