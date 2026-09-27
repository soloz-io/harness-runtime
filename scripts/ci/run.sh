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

# Retrieve this session's agent pack before the server starts.
#
# The pack holds every agent's instructions, tools and skills (waypoint
# ADR-041). SkillsManager and the tool loader resolve their directories at
# startup, so a pack that arrived later would leave this session with no skills
# and no tools while answering its health probe perfectly.
#
# Required. This image carries no agent content, so a session without a pack is a
# session with no prompts, no tools and no skills -- which starts, passes its
# health probe, and behaves as though every agent had been given an empty brief.
if [ -z "${HARNESS_PACK_URL:-}" ]; then
    echo "ERROR: HARNESS_PACK_URL is not set. This image carries no agent content;"
    echo "       the pack is fetched from the runtime that served the definition."
    exit 1
fi
echo "  HARNESS_PACK_URL: ${HARNESS_PACK_URL}"
python -c 'from core.session.pack import ensure_pack; ensure_pack()' || {
    echo "ERROR: could not retrieve the agent pack from ${HARNESS_PACK_URL}"
    exit 1
}
export HARNESS_IMAGE_DIR="${HARNESS_PACK_DIR:-/tmp/pack}/agents"
export HARNESS_TOOLS_BIN_DIR="${HARNESS_PACK_DIR:-/tmp/pack}/bin"
echo "  HARNESS_IMAGE_DIR: ${HARNESS_IMAGE_DIR} (from pack)"

# Source proxy config from Agent Vault sidecar (shared volume)
if [ -f /shared/proxy.env ]; then
    set -a
    . /shared/proxy.env
    set +a
fi

exec harness-runtime
