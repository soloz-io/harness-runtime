#!/usr/bin/env bash

################################################################################
# populate-secrets.sh
#
# Purpose: Populate HashiCorp Vault with secrets for agent_executor service
#
# Prerequisites:
#   - Vault initialized (run vault-init.sh first)
#   - Vault server running and accessible
#   - Valid Vault token with write permissions
#   - VAULT_ADDR and VAULT_TOKEN environment variables set
#
# Usage:
#   # Set Vault credentials
#   export VAULT_ADDR="http://localhost:8200"
#   export VAULT_TOKEN="your-token"
#
#   # Interactive mode (prompts for secrets)
#   ./populate-secrets.sh
#
#   # Non-interactive mode (reads from .env file)
#   ./populate-secrets.sh --from-env /path/to/.env
#
#   # Update specific secret
#   ./populate-secrets.sh --key database --value "postgresql://..."
#
# Features:
#   - Idempotent: Safe to re-run, updates existing secrets
#   - Interactive mode: Prompts for secret values
#   - File mode: Reads from .env file
#   - Validation: Checks for required secrets
#   - Secure: Passwords not echoed to terminal
#
################################################################################

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
SECRETS_PATH="secret/agent-executor"
REQUIRED_SECRETS=(
    "database_url"
    "openai_api_key"
    "langchain_api_key"
    "jwt_secret"
)

################################################################################
# Helper Functions
################################################################################

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_debug() {
    echo -e "${BLUE}[DEBUG]${NC} $1"
}

check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check vault CLI
    if ! command -v vault &> /dev/null; then
        log_error "vault CLI not found. Please install HashiCorp Vault."
        exit 1
    fi

    # Check VAULT_ADDR
    if [[ -z "${VAULT_ADDR:-}" ]]; then
        log_error "VAULT_ADDR environment variable not set."
        exit 1
    fi

    # Check VAULT_TOKEN
    if [[ -z "${VAULT_TOKEN:-}" ]]; then
        log_error "VAULT_TOKEN environment variable not set."
        exit 1
    fi

    # Test Vault connectivity
    if ! vault status &> /dev/null; then
        log_error "Cannot connect to Vault at ${VAULT_ADDR}"
        exit 1
    fi

    # Check if secrets path exists
    if ! vault kv list "${SECRETS_PATH}" &> /dev/null 2>&1; then
        log_error "Secrets path '${SECRETS_PATH}' not found."
        log_error "Run ./vault-init.sh first to initialize Vault."
        exit 1
    fi

    log_info "Prerequisites check passed."
}

read_secret() {
    local prompt="$1"
    local var_name="$2"
    local is_password="${3:-false}"

    if [[ "$is_password" == "true" ]]; then
        read -rs -p "$prompt" value
        echo "" # New line after password input
    else
        read -r -p "$prompt" value
    fi

    eval "$var_name='$value'"
}

generate_jwt_secret() {
    # Generate a secure random JWT secret
    openssl rand -base64 32 2>/dev/null || head -c 32 /dev/urandom | base64
}

read_secrets_interactive() {
    log_info "Interactive mode: Enter secrets (press Enter to skip existing)"
    echo ""

    declare -gA SECRETS

    # Database URL
    echo -e "${BLUE}Database Configuration:${NC}"
    read_secret "PostgreSQL URL (postgresql://user:pass@host:port/db): " db_url
    if [[ -n "$db_url" ]]; then
        SECRETS["database_url"]="$db_url"
    fi
    echo ""

    # OpenAI API Key
    echo -e "${BLUE}OpenAI Configuration:${NC}"
    read_secret "OpenAI API Key: " openai_key true
    if [[ -n "$openai_key" ]]; then
        SECRETS["openai_api_key"]="$openai_key"
    fi
    echo ""

    # LangChain API Key
    echo -e "${BLUE}LangChain Configuration:${NC}"
    read_secret "LangChain API Key (for tracing): " langchain_key true
    if [[ -n "$langchain_key" ]]; then
        SECRETS["langchain_api_key"]="$langchain_key"
    fi
    echo ""

    # JWT Secret
    echo -e "${BLUE}Security Configuration:${NC}"
    echo "JWT Secret (leave empty to auto-generate):"
    read_secret "JWT Secret: " jwt_secret true
    if [[ -z "$jwt_secret" ]]; then
        jwt_secret=$(generate_jwt_secret)
        log_info "Generated JWT secret automatically"
    fi
    SECRETS["jwt_secret"]="$jwt_secret"
    echo ""

    # Optional: Redis (if used for caching)
    echo -e "${BLUE}Optional: Redis Configuration${NC}"
    read_secret "Redis URL (redis://host:port, optional): " redis_url
    if [[ -n "$redis_url" ]]; then
        SECRETS["redis_url"]="$redis_url"
    fi
    echo ""

    # Optional: Sentry DSN
    echo -e "${BLUE}Optional: Monitoring Configuration${NC}"
    read_secret "Sentry DSN (optional): " sentry_dsn
    if [[ -n "$sentry_dsn" ]]; then
        SECRETS["sentry_dsn"]="$sentry_dsn"
    fi
    echo ""
}

read_secrets_from_env() {
    local env_file="$1"

    log_info "Reading secrets from ${env_file}..."

    if [[ ! -f "$env_file" ]]; then
        log_error "Environment file not found: ${env_file}"
        exit 1
    fi

    declare -gA SECRETS

    # Source the env file
    set -a
    source "$env_file"
    set +a

    # Map environment variables to Vault secrets
    [[ -n "${DATABASE_URL:-}" ]] && SECRETS["database_url"]="${DATABASE_URL}"
    [[ -n "${OPENAI_API_KEY:-}" ]] && SECRETS["openai_api_key"]="${OPENAI_API_KEY}"
    [[ -n "${LANGCHAIN_API_KEY:-}" ]] && SECRETS["langchain_api_key"]="${LANGCHAIN_API_KEY}"
    [[ -n "${JWT_SECRET:-}" ]] && SECRETS["jwt_secret"]="${JWT_SECRET}"
    [[ -n "${REDIS_URL:-}" ]] && SECRETS["redis_url"]="${REDIS_URL}"
    [[ -n "${SENTRY_DSN:-}" ]] && SECRETS["sentry_dsn"]="${SENTRY_DSN}"

    # Generate JWT secret if not provided
    if [[ -z "${SECRETS[jwt_secret]:-}" ]]; then
        SECRETS["jwt_secret"]=$(generate_jwt_secret)
        log_info "Generated JWT secret automatically"
    fi

    log_info "Loaded ${#SECRETS[@]} secrets from environment file."
}

set_single_secret() {
    local key="$1"
    local value="$2"

    declare -gA SECRETS
    SECRETS["$key"]="$value"
}

validate_secrets() {
    log_info "Validating required secrets..."

    local missing_secrets=()

    for secret in "${REQUIRED_SECRETS[@]}"; do
        if [[ -z "${SECRETS[$secret]:-}" ]]; then
            missing_secrets+=("$secret")
        fi
    done

    if [[ ${#missing_secrets[@]} -gt 0 ]]; then
        log_error "Missing required secrets:"
        for secret in "${missing_secrets[@]}"; do
            echo "  - $secret"
        done
        return 1
    fi

    log_info "All required secrets provided."
}

write_secrets_to_vault() {
    log_info "Writing secrets to Vault at ${SECRETS_PATH}/..."

    local secret_count=0

    for key in "${!SECRETS[@]}"; do
        local value="${SECRETS[$key]}"

        # Write individual secret
        vault kv put "${SECRETS_PATH}/${key}" value="${value}" > /dev/null

        log_info "✓ Written: ${key}"
        ((secret_count++))
    done

    log_info "Successfully wrote ${secret_count} secrets to Vault."
}

list_existing_secrets() {
    log_info "Existing secrets in Vault:"
    echo ""

    if vault kv list "${SECRETS_PATH}" &> /dev/null; then
        vault kv list "${SECRETS_PATH}" | tail -n +3
    else
        log_warn "No secrets found in ${SECRETS_PATH}"
    fi
    echo ""
}

verify_secrets() {
    log_info "Verifying secrets in Vault..."

    local verified=0
    local failed=0

    for secret in "${REQUIRED_SECRETS[@]}"; do
        if vault kv get "${SECRETS_PATH}/${secret}" &> /dev/null; then
            log_info "✓ Verified: ${secret}"
            ((verified++))
        else
            log_error "✗ Missing: ${secret}"
            ((failed++))
        fi
    done

    if [[ $failed -gt 0 ]]; then
        log_error "Verification failed: ${failed} secrets missing"
        return 1
    fi

    log_info "All ${verified} required secrets verified successfully."
}

show_usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Populate HashiCorp Vault with secrets for agent_executor service.

OPTIONS:
    --from-env FILE    Read secrets from .env file
    --key KEY          Set specific secret key (requires --value)
    --value VALUE      Set specific secret value (requires --key)
    --list             List existing secrets in Vault
    --verify           Verify required secrets exist
    --help             Show this help message

EXAMPLES:
    # Interactive mode
    $0

    # Load from .env file
    $0 --from-env /path/to/.env

    # Set single secret
    $0 --key database_url --value "postgresql://localhost/mydb"

    # List existing secrets
    $0 --list

    # Verify setup
    $0 --verify

ENVIRONMENT:
    VAULT_ADDR         Vault server address (required)
    VAULT_TOKEN        Vault authentication token (required)

EOF
}

################################################################################
# Main Execution
################################################################################

main() {
    local mode="interactive"
    local env_file=""
    local secret_key=""
    local secret_value=""

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --from-env)
                mode="env_file"
                env_file="$2"
                shift 2
                ;;
            --key)
                secret_key="$2"
                shift 2
                ;;
            --value)
                secret_value="$2"
                shift 2
                ;;
            --list)
                check_prerequisites
                list_existing_secrets
                exit 0
                ;;
            --verify)
                check_prerequisites
                verify_secrets
                exit 0
                ;;
            --help)
                show_usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                show_usage
                exit 1
                ;;
        esac
    done

    log_info "Starting secret population for deepagents_runtime..."
    log_info "Vault Address: ${VAULT_ADDR}"
    log_info "Secrets Path: ${SECRETS_PATH}"
    echo ""

    check_prerequisites

    # Handle single secret mode
    if [[ -n "$secret_key" ]] || [[ -n "$secret_value" ]]; then
        if [[ -z "$secret_key" ]] || [[ -z "$secret_value" ]]; then
            log_error "Both --key and --value are required for single secret mode"
            exit 1
        fi
        mode="single"
    fi

    # Read secrets based on mode
    case "$mode" in
        interactive)
            read_secrets_interactive
            ;;
        env_file)
            read_secrets_from_env "$env_file"
            ;;
        single)
            set_single_secret "$secret_key" "$secret_value"
            ;;
    esac

    # Validate and write
    if [[ "$mode" != "single" ]]; then
        validate_secrets || exit 1
    fi

    write_secrets_to_vault

    # Verify
    echo ""
    verify_secrets

    log_info "=========================================="
    log_info "Secret population complete!"
    log_info "=========================================="
    log_info ""
    log_info "Next steps:"
    log_info "1. Test Vault access from agent_executor service"
    log_info "2. Update service configuration to use Vault"
    log_info "3. Restart agent_executor service"
    log_info ""
}

main "$@"
