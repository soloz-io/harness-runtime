#!/bin/bash
set -e

# ==============================================================================
# Tier 3 CI Script: Service Entrypoint
# ==============================================================================
# Purpose: Start the agent-executor service inside the production container
# Owner: Backend Developer
# Called by: Dockerfile ENTRYPOINT
#
# Environment Variables (Container runtime):
#   - PORT: HTTP server port (default: 8080)
#   - LOG_LEVEL: Logging verbosity (default: info)
#   - POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
#   - DRAGONFLY_HOST, DRAGONFLY_PORT, DRAGONFLY_PASSWORD
#   - NATS_URL: NATS server URL (default: nats://nats.nats.svc:4222)
#   - OPENAI_API_KEY, ANTHROPIC_API_KEY
#
# Assumptions:
#   - Dependencies already installed (handled by Dockerfile)
#   - Infrastructure pre-provisioned (PostgreSQL, Dragonfly, NATS)
#   - Service connects using environment variables from Kubernetes Secrets
# ==============================================================================

# Configuration
PORT="${PORT:-8080}"
LOG_LEVEL="${LOG_LEVEL:-info}"

echo "================================================================================"
echo "Starting Agent Executor Service (CI/Production Mode)"
echo "================================================================================"
echo "  Port:           ${PORT}"
echo "  Log Level:      ${LOG_LEVEL}"
echo "  NATS URL:       ${NATS_URL:-nats://nats.nats.svc:4222}"
echo "  Postgres Host:  ${POSTGRES_HOST:-not set}"
echo "  Dragonfly Host: ${DRAGONFLY_HOST:-not set}"
echo "================================================================================"

# Validate required environment variables
if [ -z "${POSTGRES_HOST:-}" ]; then
    echo "❌ ERROR: POSTGRES_HOST environment variable is required"
    exit 1
fi

if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    echo "❌ ERROR: POSTGRES_PASSWORD environment variable is required"
    exit 1
fi

if [ -z "${DRAGONFLY_HOST:-}" ]; then
    echo "❌ ERROR: DRAGONFLY_HOST environment variable is required"
    exit 1
fi

# Start the service using uvicorn
# - Poetry is NOT used in production (dependencies pre-installed)
# - Application code at /app/ (root is deepagents_runtime package per pyproject.toml)
# - Uvicorn runs the FastAPI app from api.main:app
exec uvicorn api.main:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --log-level "${LOG_LEVEL}" \
    --no-access-log \
    --proxy-headers