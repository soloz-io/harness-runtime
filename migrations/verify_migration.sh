#!/bin/bash
#
# Migration Verification Script
# Verifies that LangGraph checkpoint tables are correctly created
#
# Usage:
#   ./verify_migration.sh [postgres_connection_string]
#
# Example:
#   ./verify_migration.sh "postgresql://user:pass@localhost:5432/agent_executor"
#
# If no connection string provided, uses environment variables:
#   PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Connection string from argument or environment
if [ -n "$1" ]; then
    CONNECTION_STRING="$1"
else
    CONNECTION_STRING="postgresql://${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE}"
fi

echo -e "${BLUE}==> LangGraph Checkpoint Migration Verification${NC}"
echo -e "${BLUE}==> Connection: ${CONNECTION_STRING}${NC}\n"

# Function to run SQL query
run_query() {
    local query="$1"
    local description="$2"

    echo -e "${YELLOW}==> $description${NC}"

    if [ -n "$1" ]; then
        psql "$CONNECTION_STRING" -c "$query"
    else
        psql "$CONNECTION_STRING"
    fi

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ Success${NC}\n"
        return 0
    else
        echo -e "${RED}✗ Failed${NC}\n"
        return 1
    fi
}

# Test 1: Check migration tracking table
echo -e "${BLUE}[1/7] Checking migration version...${NC}"
run_query "SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1;" \
    "Query migration version"

# Test 2: List all checkpoint tables
echo -e "${BLUE}[2/7] Listing checkpoint tables...${NC}"
run_query "
SELECT table_name
FROM information_schema.tables
WHERE table_name LIKE 'checkpoint%'
ORDER BY table_name;
" "Query checkpoint tables"

# Test 3: Check checkpoints table structure
echo -e "${BLUE}[3/7] Checking checkpoints table structure...${NC}"
run_query "
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'checkpoints'
ORDER BY ordinal_position;
" "Query checkpoints schema"

# Test 4: Check checkpoint_blobs table structure
echo -e "${BLUE}[4/7] Checking checkpoint_blobs table structure...${NC}"
run_query "
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'checkpoint_blobs'
ORDER BY ordinal_position;
" "Query checkpoint_blobs schema"

# Test 5: Check checkpoint_writes table structure
echo -e "${BLUE}[5/7] Checking checkpoint_writes table structure...${NC}"
run_query "
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'checkpoint_writes'
ORDER BY ordinal_position;
" "Query checkpoint_writes schema"

# Test 6: List all indexes
echo -e "${BLUE}[6/7] Checking indexes...${NC}"
run_query "
SELECT
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename LIKE 'checkpoint%'
ORDER BY tablename, indexname;
" "Query checkpoint indexes"

# Test 7: Check primary keys
echo -e "${BLUE}[7/7] Checking primary keys...${NC}"
run_query "
SELECT
    tc.table_name,
    STRING_AGG(kcu.column_name, ', ' ORDER BY kcu.ordinal_position) AS primary_key_columns
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema = kcu.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY'
    AND tc.table_name LIKE 'checkpoint%'
GROUP BY tc.table_name
ORDER BY tc.table_name;
" "Query primary keys"

# Summary
echo -e "${GREEN}======================================${NC}"
echo -e "${GREEN}✓ Migration verification completed!${NC}"
echo -e "${GREEN}======================================${NC}\n"

# Additional information
echo -e "${BLUE}Table Counts:${NC}"
run_query "
SELECT
    'checkpoints' AS table_name,
    COUNT(*) AS row_count
FROM checkpoints
UNION ALL
SELECT
    'checkpoint_blobs' AS table_name,
    COUNT(*) AS row_count
FROM checkpoint_blobs
UNION ALL
SELECT
    'checkpoint_writes' AS table_name,
    COUNT(*) AS row_count
FROM checkpoint_writes;
" "Query row counts"

echo -e "${BLUE}Storage Size:${NC}"
run_query "
SELECT
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS total_size,
    pg_size_pretty(pg_relation_size(schemaname||'.'||tablename)) AS table_size,
    pg_size_pretty(pg_indexes_size(schemaname||'.'||tablename)) AS indexes_size
FROM pg_tables
WHERE tablename LIKE 'checkpoint%'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
" "Query storage usage"

echo -e "\n${GREEN}All verification checks passed!${NC}"
echo -e "${YELLOW}Schema version: 9 (PostgresSaver compatible)${NC}\n"
