#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Starting test PostgreSQL ==="
docker compose up -d --wait
echo "PostgreSQL ready on port 5433"

echo ""
echo "=== Running DB integration tests ==="
echo "  DATABASE_URL: ${DATABASE_URL:+set}"
echo "  OPENAI_API_KEY: ${OPENAI_API_KEY:+set}"
echo "  LLM_MODEL_NAME: ${LLM_MODEL_NAME:-deepseek-v4-pro (agent definition default)}"
echo ""
cd ..
python3 -m pytest tests/test_db_checkpoint.py -v "$@"
EXIT_CODE=$?

cd "$SCRIPT_DIR"
echo ""
echo "=== Cleaning up ==="
docker compose down

exit $EXIT_CODE
