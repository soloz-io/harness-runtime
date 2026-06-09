#!/bin/bash
set -euo pipefail

# ==============================================================================
# Service CI Build Script for deepagents-runtime
# ==============================================================================
# Purpose: Build and push container image to GHCR
# Usage: ./scripts/ci/build.sh
# ==============================================================================

# Get service name from config or repo name
if [[ -f "ci/config.yaml" ]]; then
    if command -v yq &> /dev/null; then
        SERVICE_NAME=$(yq eval '.service.name' ci/config.yaml)
    else
        SERVICE_NAME="${GITHUB_REPOSITORY##*/}"
    fi
else
    SERVICE_NAME="${GITHUB_REPOSITORY##*/}"
fi

# Generate image tag
SHORT_SHA="${GITHUB_SHA:0:7}"
IMAGE_BASE="ghcr.io/${GITHUB_REPOSITORY_OWNER}/${SERVICE_NAME}"
IMAGE_TAG_SHA="${IMAGE_BASE}:sha-${SHORT_SHA}"

echo "Building image: ${IMAGE_TAG_SHA}"

# Build and push
docker buildx build \
    --platform linux/amd64 \
    --push \
    --tag "${IMAGE_TAG_SHA}" \
    --cache-from type=gha \
    --cache-to type=gha,mode=max \
    .

echo "✅ Built and pushed: ${IMAGE_TAG_SHA}"

# Output for GitHub Actions
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "image_tag=${IMAGE_TAG_SHA}" >> "$GITHUB_OUTPUT"
fi
