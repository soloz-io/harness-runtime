#!/bin/bash
set -euo pipefail

# ==============================================================================
# Service CI Entry Point for deepagents-runtime
# ==============================================================================
# Purpose: Standardized entry point for platform-based CI testing
# Usage: ./scripts/ci/in-cluster-test.sh
# ==============================================================================

# Get platform branch from service config
if [[ -f "ci/config.yaml" ]]; then
    if command -v yq &> /dev/null; then
        PLATFORM_BRANCH=$(yq eval '.platform.branch // "main"' ci/config.yaml)
    else
        PLATFORM_BRANCH="main"
    fi
else
    PLATFORM_BRANCH="main"
fi

# Always ensure fresh platform checkout
if [[ -d "zerotouch-platform" ]]; then
    echo "Removing existing platform checkout for fresh clone..."
    rm -rf zerotouch-platform
fi

echo "Cloning fresh zerotouch-platform repository (branch: $PLATFORM_BRANCH)..."
git clone -b "$PLATFORM_BRANCH" https://github.com/arun4infra/zerotouch-platform.git zerotouch-platform

# Run platform script
./zerotouch-platform/scripts/bootstrap/preview/tenants/in-cluster-test.sh