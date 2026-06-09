# Migration Deployment Guide

Quick reference for deploying LangGraph checkpoint migrations in production.

## Pre-Deployment Checklist

### 1. Verify Prerequisites

```bash
# Check namespace exists
kubectl get namespace bizmatters

# Check ServiceAccount exists
kubectl get sa agent-executor -n bizmatters

# Check Vault is accessible
kubectl get pods -n bizmatters-dev -l app=vault

# Verify PostgreSQL is running
kubectl get pods -n bizmatters -l app=postgres
```

### 2. Verify Vault Configuration

```bash
# Connect to Vault
kubectl exec -it vault-0 -n bizmatters-dev -- sh

# Inside Vault pod:
# Check Kubernetes auth is enabled
vault auth list

# Check agent-executor role exists
vault read auth/kubernetes/role/agent-executor

# Check PostgreSQL credentials exist
vault kv get kv/bizmatters/agent-executor/postgres

# Expected structure:
# {
#   "host": "postgres.bizmatters.svc.cluster.local",
#   "port": "5432",
#   "database": "agent_executor",
#   "username": "agent_executor_user",
#   "password": "secure_password"
# }
```

### 3. Create PostgreSQL Credentials in Vault (if not exists)

```bash
# Write PostgreSQL credentials to Vault
kubectl exec -it vault-0 -n bizmatters-dev -- \
  vault kv put kv/bizmatters/agent-executor/postgres \
    host="postgres.bizmatters.svc.cluster.local" \
    port="5432" \
    database="agent_executor" \
    username="agent_executor_user" \
    password="your_secure_password_here"
```

## Deployment Steps

### Step 1: Apply Migration Job

```bash
# Apply the Kubernetes Job and ConfigMap
kubectl apply -f /root/development/bizmatters/services/agent_executor/k8s/migration-job.yaml

# Verify ConfigMap created
kubectl get configmap agent-executor-migrations-001 -n bizmatters

# Verify Job created
kubectl get job agent-executor-migration-001 -n bizmatters
```

### Step 2: Monitor Job Execution

```bash
# Watch job status (wait for completion)
kubectl get jobs -n bizmatters -l app=agent-executor -w

# Expected output when complete:
# NAME                            COMPLETIONS   DURATION   AGE
# agent-executor-migration-001    1/1           45s        1m
```

### Step 3: Check Job Logs

```bash
# View init container logs (Vault authentication)
kubectl logs job/agent-executor-migration-001 -n bizmatters -c vault-init

# Expected output:
# ==> Authenticating to Vault using Kubernetes auth...
# ==> Successfully authenticated to Vault
# ==> Fetching PostgreSQL credentials from Vault...
# ==> PostgreSQL credentials fetched successfully
# export PGHOST="postgres.bizmatters.svc.cluster.local"
# export PGPORT="5432"
# export PGDATABASE="agent_executor"
# export PGUSER="agent_executor_user"

# View migration logs
kubectl logs job/agent-executor-migration-001 -n bizmatters -c migrate

# Expected output:
# ==> Loading PostgreSQL credentials from Vault...
# ==> Verifying database connection...
#     Host: postgres.bizmatters.svc.cluster.local
#     Port: 5432
#     Database: agent_executor
#     User: agent_executor_user
# ==> Database connection successful
# ==> Checking if migration has already been applied...
# ==> Applying migration 001: Create LangGraph checkpoint tables...
# CREATE TABLE
# CREATE TABLE
# CREATE TABLE
# CREATE TABLE
# CREATE INDEX
# CREATE INDEX
# CREATE INDEX
# INSERT 0 1
# ==> Migration 001 applied successfully
# ==> Verifying checkpoint tables...
# ==> All 4 checkpoint tables verified successfully
```

### Step 4: Verify Migration Success

```bash
# Check job completed successfully
kubectl get job agent-executor-migration-001 -n bizmatters

# Should show COMPLETIONS: 1/1

# Verify pod exit code
kubectl get pods -n bizmatters -l job-name=agent-executor-migration-001 \
  -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="migrate")].state.terminated.exitCode}'

# Should output: 0
```

### Step 5: Run Verification Script

```bash
# Port-forward to PostgreSQL (if needed for local testing)
kubectl port-forward -n bizmatters service/postgres 5432:5432 &

# Run verification script
export PGHOST=localhost
export PGPORT=5432
export PGDATABASE=agent_executor
export PGUSER=agent_executor_user
export PGPASSWORD=your_secure_password

cd /root/development/bizmatters/services/agent_executor/migrations
./verify_migration.sh

# Or connect directly from inside cluster:
kubectl exec -it deployment/agent-executor -n bizmatters -- \
  bash /migrations/verify_migration.sh
```

## Verification Queries

### Quick Health Check

```bash
# Execute from any pod with psql access
kubectl exec -it deployment/agent-executor -n bizmatters -- psql -c "
SELECT
    (SELECT COUNT(*) FROM checkpoints) AS checkpoints_count,
    (SELECT COUNT(*) FROM checkpoint_blobs) AS blobs_count,
    (SELECT COUNT(*) FROM checkpoint_writes) AS writes_count,
    (SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1) AS migration_version;
"
```

Expected output:
```
 checkpoints_count | blobs_count | writes_count | migration_version
-------------------+-------------+--------------+-------------------
                 0 |           0 |            0 |                 9
```

### Table Structure Check

```bash
kubectl exec -it deployment/agent-executor -n bizmatters -- psql -c "
SELECT
    table_name,
    (SELECT COUNT(*) FROM information_schema.columns WHERE table_name = t.table_name) AS column_count,
    (SELECT COUNT(*) FROM pg_indexes WHERE tablename = t.table_name) AS index_count
FROM (
    SELECT 'checkpoints' AS table_name
    UNION ALL SELECT 'checkpoint_blobs'
    UNION ALL SELECT 'checkpoint_writes'
    UNION ALL SELECT 'checkpoint_migrations'
) t;
"
```

Expected output:
```
      table_name       | column_count | index_count
-----------------------+--------------+-------------
 checkpoints           |            7 |           2
 checkpoint_blobs      |            6 |           2
 checkpoint_writes     |            9 |           2
 checkpoint_migrations |            1 |           1
```

## Cleanup (Rollback)

### Option 1: Delete Job Only (Keep Tables)

```bash
# Remove the job (tables remain)
kubectl delete job agent-executor-migration-001 -n bizmatters

# Remove the ConfigMap
kubectl delete configmap agent-executor-migrations-001 -n bizmatters
```

### Option 2: Full Rollback (Drops Tables - DANGEROUS)

**WARNING: This deletes all checkpoint data!**

```bash
# Create rollback job
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: agent-executor-migration-001-rollback
  namespace: bizmatters
spec:
  template:
    spec:
      serviceAccountName: agent-executor
      restartPolicy: Never
      containers:
      - name: rollback
        image: postgres:16-alpine
        env:
        - name: PGHOST
          value: "postgres.bizmatters.svc.cluster.local"
        - name: PGDATABASE
          value: "agent_executor"
        # Get credentials from Vault (same as migration job)
        command:
        - sh
        - -c
        - |
          # Source credentials from Vault
          source /vault/secrets/pg_env.sh

          # Run rollback SQL
          psql -f /migrations/001_create_checkpointer_tables.down.sql
      volumes:
      - name: migrations
        configMap:
          name: agent-executor-migrations-001
EOF

# Monitor rollback
kubectl logs job/agent-executor-migration-001-rollback -n bizmatters -f
```

## Troubleshooting

### Job Stuck in Pending

```bash
# Check pod events
kubectl describe job agent-executor-migration-001 -n bizmatters

# Check pod scheduling
kubectl get pods -n bizmatters -l job-name=agent-executor-migration-001

# Common issues:
# - ServiceAccount not found
# - Insufficient RBAC permissions
# - Node resource constraints
```

### Vault Authentication Failed

```bash
# Check ServiceAccount token is mounted
kubectl exec -it $(kubectl get pod -n bizmatters -l job-name=agent-executor-migration-001 -o name) \
  -c vault-init -- ls -la /var/run/secrets/kubernetes.io/serviceaccount/

# Check Vault role policy
kubectl exec -it vault-0 -n bizmatters-dev -- \
  vault read auth/kubernetes/role/agent-executor

# Verify policy allows reading postgres credentials
kubectl exec -it vault-0 -n bizmatters-dev -- \
  vault policy read agent-executor
```

### PostgreSQL Connection Failed

```bash
# Test connectivity from job pod
kubectl exec -it $(kubectl get pod -n bizmatters -l job-name=agent-executor-migration-001 -o name) \
  -c migrate -- sh -c '
    source /vault/secrets/pg_env.sh
    psql -c "SELECT version();"
  '

# Check PostgreSQL service
kubectl get svc postgres -n bizmatters

# Check PostgreSQL pod
kubectl get pods -n bizmatters -l app=postgres

# Check PostgreSQL logs
kubectl logs -n bizmatters -l app=postgres --tail=50
```

### Migration Already Applied

If you see "Migration 001 (version 9) already applied. Skipping." - this is expected behavior. The migration is idempotent.

To force re-run (will drop and recreate tables - **data loss**):

```bash
# Manually drop tables first
kubectl exec -it deployment/agent-executor -n bizmatters -- psql -c "
DROP TABLE IF EXISTS checkpoint_writes CASCADE;
DROP TABLE IF EXISTS checkpoint_blobs CASCADE;
DROP TABLE IF EXISTS checkpoints CASCADE;
DROP TABLE IF EXISTS checkpoint_migrations CASCADE;
"

# Delete and recreate job
kubectl delete job agent-executor-migration-001 -n bizmatters
kubectl apply -f k8s/migration-job.yaml
```

## Integration with CI/CD

### GitOps Deployment (ArgoCD/Flux)

Add migration job as a pre-sync hook:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  annotations:
    argocd.argoproj.io/hook: PreSync
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation
  name: agent-executor-migration-001
  # ... rest of job spec
```

### Helm Chart Integration

Create a Helm hook in `templates/migration-job.yaml`:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation
  name: {{ include "agent-executor.fullname" . }}-migration-001
  # ... rest of job spec
```

## Post-Deployment

### Enable Checkpointing in Agent Executor

Once migration is complete, the Agent Executor service can use PostgresSaver:

```python
# In agent_executor/core/executor.py
from langgraph.checkpoint.postgres import PostgresSaver
import psycopg

# Initialize checkpointer
conn = psycopg.connect(os.getenv("POSTGRES_CONNECTION_STRING"))
checkpointer = PostgresSaver(conn)

# Use in graph compilation
graph = agent_builder.build()
compiled = graph.compile(checkpointer=checkpointer)
```

### Monitor Checkpoint Usage

```bash
# Track checkpoint growth over time
kubectl exec -it deployment/agent-executor -n bizmatters -- psql -c "
SELECT
    COUNT(DISTINCT thread_id) AS active_threads,
    COUNT(*) AS total_checkpoints,
    pg_size_pretty(pg_total_relation_size('checkpoints')) AS checkpoints_size,
    pg_size_pretty(pg_total_relation_size('checkpoint_blobs')) AS blobs_size,
    pg_size_pretty(pg_total_relation_size('checkpoint_writes')) AS writes_size
FROM checkpoints;
"
```

### Set Up Backup Policy

```bash
# Example: Daily backup of checkpoint tables
kubectl create cronjob agent-executor-checkpoint-backup \
  --image=postgres:16-alpine \
  --schedule="0 2 * * *" \
  -- sh -c '
    pg_dump -h postgres.bizmatters.svc.cluster.local \
            -U agent_executor_user \
            -d agent_executor \
            -t checkpoints -t checkpoint_blobs -t checkpoint_writes \
            | gzip > /backups/checkpoint_$(date +%Y%m%d).sql.gz
  '
```

## Reference

- Migration files: `/root/development/bizmatters/services/agent_executor/migrations/`
- Kubernetes manifests: `/root/development/bizmatters/services/agent_executor/k8s/migration-job.yaml`
- Verification script: `/root/development/bizmatters/services/agent_executor/migrations/verify_migration.sh`
- Full documentation: `/root/development/bizmatters/services/agent_executor/migrations/README.md`
