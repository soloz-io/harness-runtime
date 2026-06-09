#!/bin/bash
# Downsize Dragonfly instance for preview environments
# Reduces: medium → micro (100m-500m CPU, 256Mi-1Gi RAM)
# Storage: 10GB → 5GB for Kind clusters

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

FORCE_UPDATE=false

# Parse arguments
if [ "$1" = "--force" ]; then
    FORCE_UPDATE=true
fi

# Check if this is preview mode
IS_PREVIEW_MODE=false

if [ "$FORCE_UPDATE" = true ]; then
    IS_PREVIEW_MODE=true
elif command -v kubectl > /dev/null 2>&1 && kubectl cluster-info > /dev/null 2>&1; then
    # Check if running on Kind cluster (no control-plane taints on nodes)
    if ! kubectl get nodes -o jsonpath='{.items[*].spec.taints[?(@.key=="node-role.kubernetes.io/control-plane")]}' 2>/dev/null | grep -q "control-plane"; then
        IS_PREVIEW_MODE=true
    fi
fi

if [ "$IS_PREVIEW_MODE" = true ]; then
    DRAGONFLY_CLAIM="$REPO_ROOT/platform/deepagents-runtime/base/claims/dragonfly-claim.yaml"
    
    if [ -f "$DRAGONFLY_CLAIM" ]; then
        # Downsize to micro
        if grep -q "size: medium" "$DRAGONFLY_CLAIM" 2>/dev/null; then
            sed -i.bak 's/size: medium/size: micro/g' "$DRAGONFLY_CLAIM"
            rm -f "$DRAGONFLY_CLAIM.bak"
            echo -e "${GREEN}✓${NC} Dragonfly: medium → micro (100m-500m CPU, 256Mi-1Gi RAM)"
        else
            echo -e "${YELLOW}⊘${NC} Dragonfly already at micro size"
        fi
        
        # Reduce storage for Kind clusters (minimum 5GB required)
        if grep -q "storageGB: 10" "$DRAGONFLY_CLAIM" 2>/dev/null; then
            sed -i.bak 's/storageGB: 10/storageGB: 5/g' "$DRAGONFLY_CLAIM"
            rm -f "$DRAGONFLY_CLAIM.bak"
            echo -e "${GREEN}✓${NC} Dragonfly storage: 10GB → 5GB"
        else
            echo -e "${YELLOW}⊘${NC} Dragonfly storage already optimized"
        fi
    else
        echo -e "${RED}✗${NC} Dragonfly claim not found: $DRAGONFLY_CLAIM"
        exit 1
    fi
else
    echo -e "${YELLOW}Not in preview mode - skipping Dragonfly downsizing${NC}"
fi

exit 0
