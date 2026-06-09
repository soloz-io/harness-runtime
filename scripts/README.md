# Vault Configuration Scripts

Scripts for initializing and populating HashiCorp Vault for the agent_executor service.

## Scripts

### 1. vault-init.sh

Initializes Vault infrastructure for agent_executor service.

**Features:**
- Creates KV v2 secrets engine at `secret/agent-executor`
- Configures access policy for the service
- Sets up AppRole authentication for local development
- Optionally configures Kubernetes authentication for production
- Idempotent (safe to re-run)

**Prerequisites:**
- Vault server running and accessible
- Vault CLI installed
- Vault unsealed with root token

**Usage:**
```bash
# Set Vault credentials
export VAULT_ADDR="http://localhost:8200"
export VAULT_TOKEN="your-root-token"

# Run initialization
./vault-init.sh

# With Kubernetes auth (optional)
ENABLE_K8S_AUTH=true ./vault-init.sh
```

**Output:**
- Creates AppRole credentials in `/tmp/vault-approle-credentials.txt`
- Configures policy at `agent-executor-policy`
- Sets up secrets engine at `secret/agent-executor/`

---

### 2. populate-secrets.sh

Populates Vault with application secrets.

**Features:**
- Interactive mode with secure password input
- Batch mode from .env file
- Single secret update mode
- Validates required secrets
- Auto-generates JWT secret if not provided

**Prerequisites:**
- Vault initialized (run `vault-init.sh` first)
- Valid Vault token with write permissions

**Usage:**

#### Interactive Mode
```bash
export VAULT_ADDR="http://localhost:8200"
export VAULT_TOKEN="your-token"

./populate-secrets.sh
```

#### From .env File
```bash
./populate-secrets.sh --from-env /path/to/.env
```

#### Single Secret Update
```bash
./populate-secrets.sh --key database_url --value "postgresql://localhost/mydb"
```

#### List Existing Secrets
```bash
./populate-secrets.sh --list
```

#### Verify Setup
```bash
./populate-secrets.sh --verify
```

---

## Required Secrets

The following secrets are required for agent_executor:

| Secret Key | Description | Example |
|------------|-------------|---------|
| `database_url` | PostgreSQL connection string | `postgresql://user:pass@localhost:5432/agentdb` |
| `openai_api_key` | OpenAI API key for LLM access | `sk-...` |
| `langchain_api_key` | LangChain API key for tracing | `ls__...` |
| `jwt_secret` | JWT signing secret | Auto-generated if not provided |

### Optional Secrets

| Secret Key | Description | Example |
|------------|-------------|---------|
| `redis_url` | Redis connection for caching | `redis://localhost:6379` |
| `sentry_dsn` | Sentry DSN for error tracking | `https://...@sentry.io/...` |

---

## Complete Setup Workflow

### Development Environment

```bash
# 1. Start Vault locally
vault server -dev -dev-root-token-id="dev-root-token"

# 2. In another terminal, set credentials
export VAULT_ADDR="http://localhost:8200"
export VAULT_TOKEN="dev-root-token"

# 3. Initialize Vault
./vault-init.sh

# 4. Populate secrets interactively
./populate-secrets.sh

# 5. Configure agent_executor with AppRole credentials
# Copy values from /tmp/vault-approle-credentials.txt to .env
```

### Production Environment

```bash
# 1. Set Vault production credentials
export VAULT_ADDR="https://vault.production.com"
export VAULT_TOKEN="your-production-token"

# 2. Initialize with Kubernetes auth
ENABLE_K8S_AUTH=true \
K8S_NAMESPACE="production" \
SERVICE_ACCOUNT="agent-executor" \
./vault-init.sh

# 3. Populate secrets from secure .env file
./populate-secrets.sh --from-env /secure/path/.env.production

# 4. Verify setup
./populate-secrets.sh --verify
```

---

## Vault Secret Structure

```
secret/agent-executor/
├── database_url          (PostgreSQL connection)
├── openai_api_key        (OpenAI API key)
├── langchain_api_key     (LangChain tracing)
├── jwt_secret            (JWT signing secret)
├── redis_url             (Optional: Redis)
└── sentry_dsn            (Optional: Sentry)
```

---

## Access Patterns

### Local Development (AppRole)

```python
import hvac

client = hvac.Client(url='http://localhost:8200')
client.auth.approle.login(
    role_id='role-id-from-init',
    secret_id='secret-id-from-init'
)

# Read secret
secret = client.secrets.kv.v2.read_secret_version(
    path='database_url',
    mount_point='secret/agent-executor'
)
db_url = secret['data']['data']['value']
```

### Kubernetes (ServiceAccount)

```python
import hvac

client = hvac.Client(url='https://vault.production.com')

# JWT from ServiceAccount
with open('/var/run/secrets/kubernetes.io/serviceaccount/token') as f:
    jwt = f.read()

client.auth.kubernetes.login(
    role='agent-executor',
    jwt=jwt
)

# Read secret
secret = client.secrets.kv.v2.read_secret_version(
    path='database_url',
    mount_point='secret/agent-executor'
)
```

---

## Troubleshooting

### Vault Not Accessible
```bash
# Check Vault status
vault status

# Test connectivity
curl $VAULT_ADDR/v1/sys/health
```

### Permission Denied
```bash
# Verify token has required capabilities
vault token lookup

# Check policy
vault policy read agent-executor-policy
```

### Secrets Not Found
```bash
# List all secrets
vault kv list secret/agent-executor

# Read specific secret
vault kv get secret/agent-executor/database_url
```

### AppRole Issues
```bash
# Verify AppRole exists
vault read auth/approle/role/agent-executor

# Generate new secret-id
vault write -f auth/approle/role/agent-executor/secret-id
```

---

## Security Best Practices

1. **Never commit secrets to git**
   - Add `/tmp/vault-approle-credentials.txt` to `.gitignore`
   - Store .env files securely outside repository

2. **Rotate secrets regularly**
   - Update secrets using `populate-secrets.sh --key <key> --value <new-value>`
   - Rotate AppRole secret-ids periodically

3. **Use least-privilege policies**
   - Agent only has read access to its secrets
   - No write or delete capabilities

4. **Enable audit logging in production**
   ```bash
   vault audit enable file file_path=/var/log/vault-audit.log
   ```

5. **Use Kubernetes auth in production**
   - More secure than static AppRole credentials
   - Automatic token renewal via ServiceAccount

---

## Integration with agent_executor

### Environment Variables

After initialization, configure agent_executor with:

```bash
# Vault connection
VAULT_ADDR=http://localhost:8200
VAULT_AUTH_METHOD=approle
VAULT_SECRETS_PATH=secret/agent-executor

# AppRole credentials (from vault-init.sh output)
VAULT_ROLE_ID=your-role-id
VAULT_SECRET_ID=your-secret-id
```

### Application Code

The agent_executor service uses `core/vault_client.py` to access secrets:

```python
from core.vault_client import VaultClient

vault = VaultClient()
db_url = vault.get_secret('database_url')
openai_key = vault.get_secret('openai_api_key')
```

---

## Compliance Notes

Aligns with DevOps Four Pillars:

- **Declarative**: Vault policies and config as code
- **Immutable**: Versioned secrets (KV v2)
- **Vendor-Agnostic**: Open-source HashiCorp Vault
- **Observable**: Audit logging for all secret access

---

## References

- [Vault KV v2 Secrets Engine](https://developer.hashicorp.com/vault/docs/secrets/kv/kv-v2)
- [AppRole Auth Method](https://developer.hashicorp.com/vault/docs/auth/approle)
- [Kubernetes Auth Method](https://developer.hashicorp.com/vault/docs/auth/kubernetes)
- [Vault Policies](https://developer.hashicorp.com/vault/docs/concepts/policies)
