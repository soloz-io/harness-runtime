#!/bin/bash
# Downsize Application instance for preview environments
# Reduces: medium → small (250m-1000m CPU, 512Mi-2Gi RAM)

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
    APP_DEPLOYMENT="$REPO_ROOT/platform/deepagents-runtime/overlays/pr/deployment.yaml"
    
    if [ -f "$APP_DEPLOYMENT" ]; then
        if grep -q "size: medium" "$APP_DEPLOYMENT" 2>/dev/null; then
            sed -i.bak 's/size: medium/size: small/g' "$APP_DEPLOYMENT"
            rm -f "$APP_DEPLOYMENT.bak"
            echo -e "${GREEN}✓${NC} Application: medium → small (250m-1000m CPU, 512Mi-2Gi RAM)"
        else
            echo -e "${YELLOW}⊘${NC} Application already at small size"
        fi
    else
        echo -e "${RED}✗${NC} Application deployment not found: $APP_DEPLOYMENT"
        exit 1
    fi
else
    echo -e "${YELLOW}Not in preview mode - skipping Application downsizing${NC}"
fi

exit 0
