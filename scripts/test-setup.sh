#!/bin/bash
set -euo pipefail

# ==============================================================================
# Test setup script for harness-runtime integration tests
# ==============================================================================
# Usage: ./scripts/test-setup.sh [pytest-args...]
#
# Starts all required infrastructure (PostgreSQL, Redis), exports env vars
# from harness-runtime/.env, then runs pytest.
#
# Required env vars in harness-runtime/.env:
#   AI_GATEWAY_API_KEY  — LLM gateway key
#   DATABASE_URL  — PostgreSQL connection string
#   AGENTREGISTRY_GIT_OWNER  — GitHub owner for skills repo
#   AGENTREGISTRY_GIT_REPO   — GitHub repo name for skills clone
#   AGENTREGISTRY_GITHUB_TOKEN  — GitHub token for skills clone auth
#
# Examples:
#   ./scripts/test-setup.sh                                       # all tests
#   ./scripts/test-setup.sh tests/integration_tests/skills/ -v    # skills tests
#   ./scripts/test-setup.sh tests/integration_tests/sse/ -v       # SSE tests
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR/.."
COMPOSE_DIR="$PROJECT_DIR/../waypoint/packages/waypoint-sdk/tests"
COMPOSE_PROJECT="harness-test"
HARNESS_PORT=9876

cd "$PROJECT_DIR"

# --------------------------------------------------------------------------
# Env file prerequisite — must exist with all required vars
# --------------------------------------------------------------------------
HARNESS_ENV="$PROJECT_DIR/.env"

if [ ! -f "$HARNESS_ENV" ]; then
  echo "❌ Missing: $HARNESS_ENV"
  echo "   Create it with at least:"
  echo "     AI_GATEWAY_API_KEY=sk-..."
  echo "     DATABASE_URL=postgresql://waypoint:waypoint@localhost:5433/waypoint_test"
  echo "     AGENTREGISTRY_GIT_OWNER=soloz-io"
  echo "     AGENTREGISTRY_GIT_REPO=agentregistry"
  echo "     AGENTREGISTRY_GITHUB_TOKEN=ghp_..."
  exit 1
fi

echo "✓ Found: $HARNESS_ENV"

# Load env vars (only those not already set)
while IFS='=' read -r key val; do
  case "$key" in
    ''|'#'*) continue ;;
  esac
  if [ -z "${!key:-}" ] && [ -n "$val" ]; then
    export "$key=$val"
  fi
done < <(grep -vE '^\s*(#|$)' "$HARNESS_ENV" || true)

# --------------------------------------------------------------------------
# Fail early: validate every required env var has a value
# --------------------------------------------------------------------------
MISSING=""
[ -z "${AI_GATEWAY_API_KEY:-}" ]           && MISSING="$MISSING AI_GATEWAY_API_KEY"
[ -z "${DATABASE_URL:-}" ]                 && MISSING="$MISSING DATABASE_URL"
[ -z "${AGENTREGISTRY_GIT_OWNER:-}" ]      && MISSING="$MISSING AGENTREGISTRY_GIT_OWNER"
[ -z "${AGENTREGISTRY_GIT_REPO:-}" ]       && MISSING="$MISSING AGENTREGISTRY_GIT_REPO"
[ -z "${AGENTREGISTRY_GITHUB_TOKEN:-}" ]   && MISSING="$MISSING AGENTREGISTRY_GITHUB_TOKEN"

if [ -n "$MISSING" ]; then
  echo ""
  echo "❌ Missing required env vars after loading .env:$MISSING"
  echo ""
  echo "   Add them to $HARNESS_ENV"
  exit 1
fi

echo "✓ All required env vars are set"

# --------------------------------------------------------------------------
# Kill any process on harness port
# --------------------------------------------------------------------------
if lsof -i ":$HARNESS_PORT" &>/dev/null 2>&1; then
  echo "→ Killing process on port $HARNESS_PORT..."
  lsof -ti ":$HARNESS_PORT" | xargs kill -9 2>/dev/null || true
fi

# --------------------------------------------------------------------------
# Ensure Redis is running (required by cli.py event bus)
# --------------------------------------------------------------------------
if ! redis-cli ping &>/dev/null; then
  echo "→ Starting Redis..."
  redis-server --daemonize yes --port 6379 &>/dev/null
  sleep 1
  if redis-cli ping &>/dev/null; then
    echo "✓ Redis started"
  else
    echo "❌ Failed to start Redis. Install with: brew install redis"
    exit 1
  fi
else
  echo "✓ Redis already running"
fi

# --------------------------------------------------------------------------
# Start PostgreSQL (port 5433)
# --------------------------------------------------------------------------
if [ ! -f "$COMPOSE_DIR/docker-compose.yml" ]; then
  echo "❌ Docker compose file not found at $COMPOSE_DIR/docker-compose.yml"
  exit 1
fi

echo "→ Starting PostgreSQL..."
docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_DIR/docker-compose.yml" up -d postgres 2>&1 | tail -1

# Wait for PostgreSQL to be healthy
echo "→ Waiting for PostgreSQL..."
for i in $(seq 1 30); do
  if pg_isready -h localhost -p 5433 -U waypoint &>/dev/null; then
    echo "✓ PostgreSQL ready (port 5433)"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "❌ PostgreSQL did not become ready within 30s"
    docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_DIR/docker-compose.yml" logs postgres 2>&1 | tail -20
    exit 1
  fi
  sleep 1
done

# --------------------------------------------------------------------------
# Initialize database tables required by integration tests
# --------------------------------------------------------------------------
echo "→ Initializing database tables..."
DB_SETUP="$PROJECT_DIR/tests/db_setup.sql"
if [ -f "$DB_SETUP" ]; then
  PGPASSWORD=waypoint psql -h localhost -p 5433 -U waypoint -d waypoint_test -f "$DB_SETUP" 2>&1 | tail -1
  echo "✓ Database initialized"
else
  echo "⚠️  db_setup.sql not found at $DB_SETUP — some tests may fail"
fi

# --------------------------------------------------------------------------
# Cleanup trap
# --------------------------------------------------------------------------
cleanup() {
  echo ""
  echo "→ Cleaning up..."
  docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_DIR/docker-compose.yml" down &>/dev/null || true
  echo "✓ Cleanup done"
}
trap cleanup EXIT

# --------------------------------------------------------------------------
# Run tests
# --------------------------------------------------------------------------
echo ""
echo "========== Running tests: ${*:-"all"} =========="
echo ""

PYTHONPATH="." uv run pytest "$@" -v --timeout 120

echo ""
echo "✅ Tests complete. Exit code: $?"
