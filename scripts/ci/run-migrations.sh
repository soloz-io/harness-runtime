#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Database Migrations Script - deepagents-runtime
# ==============================================================================
# Runs database migrations for deepagents-runtime service
# Used by ArgoCD PreSync hooks and CI workflows
# ==============================================================================

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $*" >&2; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $*" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

main() {
    log_info "Starting database migrations for deepagents-runtime..."
    
    # Validate required environment variables
    required_vars=("POSTGRES_HOST" "POSTGRES_PORT" "POSTGRES_DB" "POSTGRES_USER" "POSTGRES_PASSWORD")
    for var in "${required_vars[@]}"; do
        if [[ -z "${!var:-}" ]]; then
            log_error "Required environment variable not set: $var"
            return 1
        fi
    done
    
    log_info "Database connection: ${POSTGRES_USER}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
    
    # Wait for PostgreSQL to be ready
    log_info "Waiting for PostgreSQL to be ready..."
    until pg_isready -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER"; do
        echo "PostgreSQL not ready, waiting..."
        sleep 2
    done
    
    log_success "PostgreSQL is ready"
    
    # Set migration directory
    MIGRATION_DIR="${MIGRATION_DIR:-/app/migrations}"
    
    if [[ ! -d "$MIGRATION_DIR" ]]; then
        log_error "Migration directory not found: $MIGRATION_DIR"
        return 1
    fi
    
    log_info "Running migrations from: $MIGRATION_DIR"
    
    # Run each migration file in order
    migration_count=0
    for migration in "$MIGRATION_DIR"/*.up.sql; do
        if [[ -f "$migration" ]]; then
            log_info "Running migration: $(basename "$migration")"
            PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$migration"
            migration_count=$((migration_count + 1))
        fi
    done
    
    if [[ $migration_count -eq 0 ]]; then
        log_info "No migration files found in $MIGRATION_DIR"
    else
        log_success "Applied $migration_count migrations successfully"
    fi
    
    log_success "Database migrations completed for deepagents-runtime"
}

main "$@"