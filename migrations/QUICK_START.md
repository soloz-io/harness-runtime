# Quick Start: Deploy LangGraph Checkpoint Migration

5-minute deployment guide for DevOps engineers.

## Prerequisites Checklist

```bash
# 1. Verify namespace exists
kubectl get namespace bizmatters

# 2. Verify ServiceAccount exists
kubectl get sa agent-executor -n bizmatters

# 3. Verify Vault is running
kubectl get pods -n bizmatters-dev -l app=vault

# 4. Verify PostgreSQL is accessible
kubectl get pods -n bizmatters -l app=postgres
```

## Step 1: Configure Vault (One-Time Setup)

```bash
# Connect to Vault pod
kubectl exec -it vault-0 -n bizmatters-dev -- sh

# Inside Vault, enable Kubernetes auth (if not already enabled)
vault auth enable kubernetes

# Configure Kubernetes auth to connect to K8s API
vault write auth/kubernetes/config \
    kubernetes_host="https://kubernetes.default.svc:443" \
    kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
    token_reviewer_jwt=@/var/run/secrets/kubernetes.io/serviceaccount/token

# Create policy for agent-executor (read PostgreSQL credentials)
vault policy write agent-executor-policy - <<EOF
path "kv/data/bizmatters/agent-executor/postgres" {
  capabilities = ["read"]
}
EOF

# Create Kubernetes auth role
vault write auth/kubernetes/role/agent-executor \
    bound_service_account_names=agent-executor \
    bound_service_account_namespaces=bizmatters \
    policies=agent-executor-policy \
    ttl=1h

# Store PostgreSQL credentials in Vault
vault kv put kv/bizmatters/agent-executor/postgres \
    host="postgres.bizmatters.svc.cluster.local" \
    port="5432" \
    database="agent_executor" \
    username="agent_executor_user" \
    password="YOUR_SECURE_PASSWORD_HERE"

# Exit Vault pod
exit
```

## Step 2: Deploy Migration Job

```bash
# Apply migration job and ConfigMap
kubectl apply -f /root/development/bizmatters/services/agent_executor/k8s/migration-job.yaml

# Expected output:
# job.batch/agent-executor-migration-001 created
# configmap/agent-executor-migrations-001 created
```

## Step 3: Monitor Execution

```bash
# Watch job status (wait for COMPLETIONS 1/1)
kubectl get job agent-executor-migration-001 -n bizmatters --watch

# In another terminal, follow logs
kubectl logs job/agent-executor-migration-001 -n bizmatters -f

# Expected final log lines:
# ==> Migration 001 applied successfully
# ==> All 4 checkpoint tables verified successfully
```

## Step 4: Verify Success

```bash
# Check job completed successfully
kubectl get job agent-executor-migration-001 -n bizmatters

# Should show:
# NAME                            COMPLETIONS   DURATION   AGE
# agent-executor-migration-001    1/1           45s        2m

# Verify tables were created
kubectl exec -it deployment/agent-executor -n bizmatters -- \
  psql -c "SELECT table_name FROM information_schema.tables
           WHERE table_name LIKE 'checkpoint%' ORDER BY table_name;"

# Expected output:
#       table_name
# ----------------------
#  checkpoint_blobs
#  checkpoint_migrations
#  checkpoint_writes
#  checkpoints
# (4 rows)

# Check migration version
kubectl exec -it deployment/agent-executor -n bizmatters -- \
  psql -c "SELECT v FROM checkpoint_migrations;"

# Expected output:
#  v
# ---
#  9
# (1 row)
```

## Step 5: Run Automated Verification (Optional)

```bash
# Run verification script from inside cluster
kubectl exec -it deployment/agent-executor -n bizmatters -- bash -c '
  cd /migrations
  ./verify_migration.sh
'

# Or run locally (requires port-forward)
kubectl port-forward -n bizmatters service/postgres 5432:5432 &
export PGHOST=localhost PGPORT=5432 PGDATABASE=agent_executor
export PGUSER=agent_executor_user PGPASSWORD=your_password

cd /root/development/bizmatters/services/agent_executor/migrations
./verify_migration.sh
```

## Troubleshooting

### Job Fails: Vault Authentication Error

```bash
# View init container logs
kubectl logs job/agent-executor-migration-001 -n bizmatters -c vault-init

# Common issues:
# - ServiceAccount doesn't exist
# - Vault role not configured
# - RBAC permissions missing

# Fix: Re-run Step 1 (Vault configuration)
```

### Job Fails: PostgreSQL Connection Error

```bash
# View migration logs
kubectl logs job/agent-executor-migration-001 -n bizmatters -c migrate

# Test PostgreSQL connectivity
kubectl run psql-test --rm -it --image=postgres:16-alpine -n bizmatters -- \
  psql -h postgres.bizmatters.svc.cluster.local \
       -p 5432 \
       -U agent_executor_user \
       -d agent_executor \
       -c "SELECT version();"

# Common issues:
# - PostgreSQL not running
# - Wrong credentials in Vault
# - Network policy blocking connection
```

### Job Succeeds But Tables Not Created

```bash
# Describe job for events
kubectl describe job agent-executor-migration-001 -n bizmatters

# Check pod exit code
kubectl get pods -n bizmatters -l job-name=agent-executor-migration-001 \
  -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="migrate")].state.terminated.exitCode}'

# Should be: 0

# If non-zero, check full logs:
kubectl logs job/agent-executor-migration-001 -n bizmatters -c migrate --tail=100
```

## Cleanup (After Verification)

```bash
# Optional: Remove job (tables remain intact)
kubectl delete job agent-executor-migration-001 -n bizmatters

# Optional: Remove ConfigMap
kubectl delete configmap agent-executor-migrations-001 -n bizmatters
```

## Rollback (DANGEROUS - Deletes All Checkpoint Data)

```bash
# Connect to PostgreSQL and run rollback SQL
kubectl exec -it deployment/agent-executor -n bizmatters -- \
  psql -f /migrations/001_create_checkpointer_tables.down.sql

# Verify tables dropped
kubectl exec -it deployment/agent-executor -n bizmatters -- \
  psql -c "\dt checkpoint*"

# Should return: Did not find any relations.
```

## CI/CD Integration

### Helm Pre-Install Hook

Add to `templates/migration-job.yaml`:

```yaml
metadata:
  annotations:
    "helm.sh/hook": pre-install,pre-upgrade
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation
```

### ArgoCD PreSync Hook

Add to `migration-job.yaml`:

```yaml
metadata:
  annotations:
    argocd.argoproj.io/hook: PreSync
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation
```

## Summary

1. Configure Vault (one-time setup)
2. Apply migration job: `kubectl apply -f k8s/migration-job.yaml`
3. Monitor: `kubectl logs job/agent-executor-migration-001 -n bizmatters -f`
4. Verify: Check job completion and query tables
5. Done! Agent Executor can now use PostgreSQL checkpointing

## Next Steps

- Review full documentation: `migrations/README.md`
- Deployment details: `migrations/DEPLOYMENT_GUIDE.md`
- Implementation summary: `migrations/IMPLEMENTATION_SUMMARY.md`

## Support

For issues or questions:
- Check troubleshooting section in `migrations/README.md`
- Review Kubernetes job logs
- Verify Vault configuration
- Test PostgreSQL connectivity
