# Agent Executor Database Migrations

This directory contains database migration scripts for the Agent Executor service's PostgreSQL checkpoint storage.

## Overview

The Agent Executor uses **LangGraph's PostgresSaver** for checkpoint persistence. These migrations create the exact schema required by the `langgraph-checkpoint-postgres` library.

## Migration Files

### Migration 001: Create Checkpoint Tables

**Purpose:** Initialize the LangGraph checkpoint storage schema

**Files:**
- `001_create_checkpointer_tables.up.sql` - Creates tables and indexes
- `001_create_checkpointer_tables.down.sql` - Rolls back (drops tables)

**Tables Created:**

1. **checkpoint_migrations** - Tracks applied migration versions
2. **checkpoints** - Stores full checkpoint state per thread
3. **checkpoint_blobs** - Stores channel values separately (optimization)
4. **checkpoint_writes** - Stores intermediate writes (fault tolerance)

**Schema Version:** Migration implements PostgresSaver v9 schema

## Running Migrations

### Option 1: Kubernetes Job (Recommended for Production)

The migration job runs automatically before service deployment using Kubernetes Job.

```bash
# Apply the migration job
kubectl apply -f /root/development/bizmatters/services/agent_executor/k8s/migration-job.yaml

# Watch job status
kubectl get jobs -n bizmatters -l app=agent-executor -w

# View migration logs
kubectl logs -n bizmatters job/agent-executor-migration-001 -c migrate

# Check job completion
kubectl get job agent-executor-migration-001 -n bizmatters
```

**Job Behavior:**
- Fetches PostgreSQL credentials from Vault using Kubernetes auth
- Checks if migration already applied (idempotent)
- Applies migration SQL
- Verifies table creation
- Exits with status 0 on success, non-zero on failure

### Option 2: Direct psql (Development/Testing)

For local development or manual migration:

```bash
# Set PostgreSQL connection variables
export PGHOST=localhost
export PGPORT=5432
export PGDATABASE=agent_executor
export PGUSER=postgres
export PGPASSWORD=your_password

# Apply migration
psql -f /root/development/bizmatters/services/agent_executor/migrations/001_create_checkpointer_tables.up.sql

# Rollback (if needed)
psql -f /root/development/bizmatters/services/agent_executor/migrations/001_create_checkpointer_tables.down.sql
```

### Option 3: Python (Using PostgresSaver.setup())

The LangGraph library provides a built-in setup method:

```python
from langgraph.checkpoint.postgres import PostgresSaver
import psycopg

# Connect to database
conn = psycopg.connect(
    host="localhost",
    port=5432,
    dbname="agent_executor",
    user="postgres",
    password="your_password"
)

# Initialize checkpointer (creates tables automatically)
checkpointer = PostgresSaver(conn)
checkpointer.setup()  # Applies all migrations

conn.close()
```

## Verification

### Check Migration Status

```bash
# Verify migration tracking table
kubectl exec -it deployment/agent-executor -n bizmatters -- \
  psql -c "SELECT * FROM checkpoint_migrations;"

# Expected output:
#  v
# ---
#  9
```

### Verify All Tables Created

```bash
# List checkpoint tables
kubectl exec -it deployment/agent-executor -n bizmatters -- \
  psql -c "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_name LIKE 'checkpoint%'
    ORDER BY table_name;
  "

# Expected output:
#       table_name
# ----------------------
#  checkpoint_blobs
#  checkpoint_migrations
#  checkpoint_writes
#  checkpoints
```

### Verify Table Structure

```bash
# Check checkpoints table schema
kubectl exec -it deployment/agent-executor -n bizmatters -- \
  psql -c "\d checkpoints"

# Check indexes
kubectl exec -it deployment/agent-executor -n bizmatters -- \
  psql -c "
    SELECT indexname, indexdef
    FROM pg_indexes
    WHERE tablename LIKE 'checkpoint%';
  "
```

### Test Checkpoint Storage

```python
# Test writing a checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
import psycopg
from uuid import uuid4

conn = psycopg.connect("your_connection_string")
checkpointer = PostgresSaver(conn)

# Write test checkpoint
thread_id = str(uuid4())
checkpoint_id = str(uuid4())

checkpointer.put(
    config={
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id
        }
    },
    checkpoint={
        "v": 1,
        "id": checkpoint_id,
        "ts": "2025-11-12T00:00:00.000Z",
        "channel_values": {"messages": []},
        "channel_versions": {"messages": "v1"},
        "versions_seen": {}
    },
    metadata={"source": "test"}
)

# Retrieve checkpoint
retrieved = checkpointer.get(config={
    "configurable": {
        "thread_id": thread_id,
        "checkpoint_ns": ""
    }
})

print(f"Checkpoint stored and retrieved: {retrieved is not None}")
conn.close()
```

## Migration Job Architecture

### Security Model

1. **Kubernetes ServiceAccount:** `agent-executor`
   - Mounted at `/var/run/secrets/kubernetes.io/serviceaccount/token`
   - Used for Vault authentication

2. **Vault Authentication:**
   - Method: Kubernetes auth (`auth/kubernetes/login`)
   - Role: `agent-executor`
   - Auto-authenticates using ServiceAccount JWT

3. **Secret Storage:**
   - Vault path: `kv/bizmatters/agent-executor/postgres`
   - Credentials written to tmpfs volume (never disk)
   - Shared between init and main container via emptyDir

### Container Flow

```
┌─────────────────────────────────────────────────────────────┐
│ Init Container: vault-init                                  │
│ • Authenticates to Vault using ServiceAccount JWT          │
│ • Fetches PostgreSQL credentials                           │
│ • Writes credentials to /vault/secrets (tmpfs)             │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ Main Container: migrate                                     │
│ • Sources credentials from /vault/secrets                   │
│ • Connects to PostgreSQL                                    │
│ • Checks if migration already applied (idempotent)         │
│ • Applies migration SQL                                     │
│ • Verifies tables created                                   │
│ • Exits with status code                                    │
└─────────────────────────────────────────────────────────────┘
```

## Troubleshooting

### Job Failed: Cannot Connect to Vault

```bash
# Check Vault is running
kubectl get pods -n bizmatters-dev -l app=vault

# Check ServiceAccount exists
kubectl get sa agent-executor -n bizmatters

# Check Vault role configuration
kubectl exec -it vault-0 -n bizmatters-dev -- \
  vault read auth/kubernetes/role/agent-executor
```

### Job Failed: Cannot Connect to PostgreSQL

```bash
# Verify PostgreSQL credentials in Vault
kubectl exec -it vault-0 -n bizmatters-dev -- \
  vault kv get kv/bizmatters/agent-executor/postgres

# Test PostgreSQL connectivity
kubectl run psql-test --rm -it --image=postgres:16-alpine -- \
  psql -h <PGHOST> -p <PGPORT> -U <PGUSER> -d <PGDATABASE> -c "SELECT version();"
```

### Migration Already Applied

The migration is **idempotent**. If tables already exist, the job will:
1. Check `checkpoint_migrations` table for version 9
2. Skip migration if already applied
3. Exit successfully

To force re-run (dangerous - drops all data):

```bash
# Manually rollback first
kubectl exec -it deployment/agent-executor -n bizmatters -- \
  psql -f /migrations/001_create_checkpointer_tables.down.sql

# Then re-run job
kubectl delete job agent-executor-migration-001 -n bizmatters
kubectl apply -f k8s/migration-job.yaml
```

### View Detailed Logs

```bash
# View init container logs (Vault authentication)
kubectl logs job/agent-executor-migration-001 -n bizmatters -c vault-init

# View main container logs (migration execution)
kubectl logs job/agent-executor-migration-001 -n bizmatters -c migrate

# Describe job for events
kubectl describe job agent-executor-migration-001 -n bizmatters
```

## Schema Details

### checkpoints Table

Stores the complete checkpoint state for each thread.

| Column | Type | Description |
|--------|------|-------------|
| thread_id | TEXT | Unique identifier for conversation thread |
| checkpoint_ns | TEXT | Namespace for checkpoint isolation (default: '') |
| checkpoint_id | TEXT | Unique checkpoint identifier |
| parent_checkpoint_id | TEXT | Previous checkpoint ID (for history) |
| type | TEXT | Checkpoint type metadata |
| checkpoint | JSONB | Full checkpoint state (serialized) |
| metadata | JSONB | User-defined metadata |

**Primary Key:** (thread_id, checkpoint_ns, checkpoint_id)

### checkpoint_blobs Table

Stores individual channel values separately for optimization. Only changed values are stored per checkpoint.

| Column | Type | Description |
|--------|------|-------------|
| thread_id | TEXT | Thread identifier |
| checkpoint_ns | TEXT | Namespace |
| channel | TEXT | Channel name (e.g., "messages", "context") |
| version | TEXT | Channel value version |
| type | TEXT | Channel data type |
| blob | BYTEA | Serialized channel value |

**Primary Key:** (thread_id, checkpoint_ns, channel, version)

### checkpoint_writes Table

Stores intermediate writes during graph execution. Critical for fault tolerance.

| Column | Type | Description |
|--------|------|-------------|
| thread_id | TEXT | Thread identifier |
| checkpoint_ns | TEXT | Namespace |
| checkpoint_id | TEXT | Associated checkpoint |
| task_id | TEXT | Node/task identifier |
| idx | INTEGER | Write sequence number |
| channel | TEXT | Target channel |
| type | TEXT | Write data type |
| blob | BYTEA | Serialized write value |
| task_path | TEXT | Task execution path |

**Primary Key:** (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)

### Indexes

Performance indexes on `thread_id` for all tables:
- `checkpoints_thread_id_idx`
- `checkpoint_blobs_thread_id_idx`
- `checkpoint_writes_thread_id_idx`

## References

- [LangGraph Checkpoint Postgres](https://github.com/langchain-ai/langgraph/tree/main/libs/checkpoint-postgres)
- [PostgresSaver Documentation](https://langchain-ai.github.io/langgraphjs/reference/classes/checkpoint_postgres.PostgresSaver.html)
- [Agent Executor Design Doc](/.kiro/specs/agent-builder/phase1-9-agent_executor_service/design.md)
