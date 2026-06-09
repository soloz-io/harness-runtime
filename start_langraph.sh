#!/bin/bash
#
# start_langraph.sh - Development mode startup script for Agent Executor
#
# This script starts LangGraph CLI for development and testing of the agent executor.
#
# Usage:
#   ./start_langraph.sh [PORT]
#
# Examples:
#   ./start_langraph.sh          # Start on default port 8125
#   ./start_langraph.sh 8000     # Start on port 8000

set -e

# Change to script directory
cd "$(dirname "${BASH_SOURCE[0]}")"

# --- Development Mode Startup ---
clear
echo "======================================================="
echo "  Starting Agent Executor LangGraph Development Mode"
echo "======================================================="
echo ""

# Set default port for development (different from spec-engine's 8124)
DEFAULT_PORT=8125
PORT=${1:-$DEFAULT_PORT}

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "ERROR: Virtual environment not found at .venv"
    echo "Please run: uv sync"
    exit 1
fi

# Validate that LangGraph CLI is installed
LANGGRAPH_EXEC="./.venv/bin/langgraph"
if [ ! -f "$LANGGRAPH_EXEC" ]; then
    echo "ERROR: LangGraph CLI not found at '$LANGGRAPH_EXEC'"
    echo "Please install it: uv pip install langgraph-cli"
    exit 1
fi

# Display startup information
echo ""
echo "✓ LangGraph Playground will be available at: http://localhost:${PORT}"
echo "✓ Studio UI: https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:${PORT}"
echo ""
echo "Starting LangGraph CLI in development mode on port ${PORT}..."
echo "Using --allow-blocking flag to handle synchronous I/O operations..."
echo ""

# Start LangGraph CLI with development configuration
exec "$LANGGRAPH_EXEC" dev --port "$PORT" --allow-blocking
