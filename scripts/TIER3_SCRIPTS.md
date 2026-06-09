# deepagents-runtime - 2-Tier Scripts

This directory contains scripts for the `deepagents-runtime` service using a simplified 2-tier architecture.

## Script Architecture Overview

| Tier | Location | Owner | Purpose |
|------|----------|-------|---------|
| **Tier 1** | `.github/workflows/` | DevOps | Pipeline definitions (GitHub Actions) |
| **Tier 2** | `scripts/ci/` & `scripts/local/` | Backend Developer | Service operations |

This directory contains **Tier 2 scripts** owned by the Backend Developer.

---

## Directory Structure

```
scripts/
├── TIER3_SCRIPTS.md       # This file (renamed from old structure)
├── ci/                    # CI/Production scripts
│   ├── build.sh           # Build and load Docker image into Kind
│   ├── deploy.sh          # Deploy service to Kubernetes cluster
│   ├── test.sh            # Run integration tests
│   ├── run.sh             # Container entrypoint (production)
│   └── run-migrations.sh  # Database migrations
└── local/                 # Local development scripts
    ├── build.sh           # Build Docker image locally
    ├── run.sh             # Start service with hot-reload
    └── test.sh            # Run all tests locally
```

**Note**: This is a simplified 2-tier architecture where Tier 1 (GitHub Actions) directly calls Tier 2 scripts.

---

## CI Scripts (Production/CI Environment)

### `ci/build.sh`

**Purpose:** Build Docker image for testing (Kind) or production (Registry push).

**Usage:**
```bash
# Test mode (integration testing)
./scripts/ci/build.sh --mode=test

# Production mode (registry push)
./scripts/ci/build.sh --mode=production
```

**Modes:**
- **`test`**: Builds image and loads into Kind cluster for integration testing
- **`production`**: Builds image and pushes to GitHub Container Registry

**Environment Variables:**
- `GITHUB_SHA` (required for production): Git commit SHA
- `GITHUB_REF_NAME` (required for production): Git branch or tag name
- `GITHUB_OUTPUT` (optional): GitHub Actions output file

**Output (Test Mode):**
- Builds Docker image: `deepagents-runtime:ci-test`
- Loads image into Kind cluster: `zerotouch-preview`

**Output (Production Mode):**
- Builds multi-platform Docker image
- Pushes to `ghcr.io/arun4infra/deepagents-runtime` with appropriate tags
- Updates deployment manifest for main branch

**Called by:** GitHub Actions workflows

### `ci/deploy.sh`

**Purpose:** Deploy deepagents-runtime service to Kubernetes cluster.

**Usage:**
```bash
# Called by GitHub Actions workflow
./scripts/ci/deploy.sh
```

**Environment Variables:**
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` (for ESO)

**Output:**
- Creates namespace: `intelligence-deepagents`
- Applies platform claims (ExternalSecrets, PostgreSQL, Dragonfly)
- Deploys EventDrivenService with built image
- Waits for pod to be ready

**Called by:** GitHub Actions workflow

### `ci/test.sh`

**Purpose:** Execute integration tests against deployed service.

**Usage:**
```bash
# Called by GitHub Actions workflow
./scripts/ci/test.sh
```

**Environment Variables:**
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` (required for LLM calls)

**Output:**
- Runs pytest integration tests
- Generates test results and coverage reports
- Collects debugging artifacts

**Called by:** GitHub Actions workflow

---

### `ci/run.sh`

**Purpose:** Container entrypoint for starting the service in production/CI.

**Usage:**
```bash
# Called by Dockerfile ENTRYPOINT (not invoked manually)
ENTRYPOINT ["/app/scripts/ci/run.sh"]
```

**Environment Variables:**
- `PORT` (required): HTTP server port (default: `8080`)
- `LOG_LEVEL` (optional): Logging verbosity (default: `info`)
- All secrets injected via ESO and Crossplane (no manual configuration)

**Behavior:**
- Validates required environment variables
- Starts uvicorn without Poetry (dependencies pre-installed)
- Connects to infrastructure via environment variables
- Respects `$PORT` environment variable for container runtime

**Called by:** Docker/Kubernetes container runtime

**Notes:**
- This script runs **inside** the container
- Dependencies must be pre-installed (handled by Dockerfile)
- Never manages infrastructure (that's Tier 2/DevOps)

---

### `ci/run-tests.sh`

**Purpose:** Execute E2E tests in CI environment (inside test runner pod).

**Usage:**
```bash
# Inside test runner pod (called by Tier 2 orchestrator)
./services/agent_executor/scripts/ci/run-tests.sh
```

**Environment Variables:**
- `NATS_URL` (required): NATS server connection string
- `TEST_POSTGRES_URI` (required): PostgreSQL connection for monitoring
- `TEST_REDIS_URL` (required): Redis connection for monitoring
- `NATS_URL` (required): NATS connection URL
- `TESTS_PATH` (optional): Path to E2E test file (default: `/root/development/bizmatters/tests/e2e/test_agent_executor_e2e.py`)

**Behavior:**
- Validates all required environment variables
- Runs pytest on E2E test suite
- Exits with pytest exit code (0 = success, 1 = failure)

**Called by:** Tier 2 orchestration scripts via `kubectl exec` or Kubernetes Job

**Example:**
```bash
kubectl exec -n langgraph test-runner -- /app/scripts/ci/run-tests.sh
```

---

### `ci/run-migrations.sh`

**Purpose:** Execute PostgreSQL database migrations for the agent-executor service.

**Usage:**
```bash
# CI environment (credentials from environment variables)
POSTGRES_PASSWORD="password" ./services/agent_executor/scripts/ci/run-migrations.sh

# Or with custom configuration
POSTGRES_HOST="custom-host" \
POSTGRES_DB="custom_db" \
POSTGRES_PASSWORD="password" \
./services/agent_executor/scripts/ci/run-migrations.sh
```

**Environment Variables:**
- `POSTGRES_HOST` - PostgreSQL host (default: postgresql.bizmatters-dev.svc.cluster.local)
- `POSTGRES_PORT` - PostgreSQL port (default: 5432)
- `POSTGRES_DB` - Database name (default: langgraph_dev)
- `POSTGRES_USER` - Database user (default: postgres)
- `POSTGRES_PASSWORD` - Database password (required)
- `MIGRATION_DIR` - Path to migration files (default: ./migrations)

**Behavior:**
- Executes all `*.up.sql` migration files in order
- Creates `agent_executor` schema
- Creates LangGraph checkpoint tables (checkpoints, checkpoint_migrations, checkpoint_blobs, checkpoint_writes)
- Exits on first failure

**Called By:**
- Tier 2 orchestration scripts (`/scripts/services/run-e2e-for-pr.sh`)
- CI/CD pipelines (`.github/workflows/`)
- Kubernetes Jobs (migration Job manifest)

**Example:**
```bash
# From Kubernetes Job
kubectl exec -n bizmatters-dev migration-job -- \
  /app/scripts/ci/run-migrations.sh
```

**Notes:**
- Requires `psql` client installed in container
- Migration files are idempotent (safe to run multiple times)
- Schema and tables use `IF NOT EXISTS` clauses

---

## Local Scripts (Development Environment)

### `local/run.sh`

**Purpose:** Start the service in local development mode with hot-reload.

**Usage:**
```bash
# From service directory
cd services/agent_executor
./scripts/local/run.sh
```

**Features:**
- Hot-reload enabled (auto-restart on code changes)
- Loads `.env` file if present
- Uses Poetry-managed virtual environment
- Default port: 8080
- Access API docs at: `http://localhost:8080/docs`

**Environment Variables:**
- `PORT` (optional): HTTP server port (default: `8080`)
- `LOG_LEVEL` (optional): Logging verbosity (default: `debug`)
- All secrets loaded from `.env` file for local development

**Called by:** Developer via terminal (NEVER by CI)

**Notes:**
- Requires Poetry installed
- Optional: Uncomment Docker Compose section to start local infrastructure

---

### `local/build.sh`

**Purpose:** Build Docker image for local development with caching.

**Usage:**
```bash
# From deepagents-runtime directory
./scripts/local/build.sh
```

**Features:**
- Uses Docker layer caching for speed
- Tags as `local-{git-sha}` and `latest`
- Does NOT push to any registry
- Compatible with Docker Desktop Kubernetes

**Called by:** Developer via terminal

### `local/test.sh`

**Purpose:** Run all tests locally (unit + integration).

**Usage:**
```bash
# From deepagents-runtime directory
./scripts/local/test.sh
```

**Test Stages:**
1. **Unit tests** (`tests/unit/`): Fast, isolated tests with code coverage
2. **Integration tests** (`tests/integration/`): Tests with external dependencies

**Features:**
- Color-coded output (green = pass, red = fail, yellow = warning)
- Code coverage report (HTML + terminal)
- Automatic Docker Compose management for test infrastructure

**Environment Variables:**
- `TESTING=true`: Automatically set by script
- `LOG_LEVEL` (optional): Logging verbosity (default: `info`)
- All secrets loaded from `.env` file for local testing

**Output:**
- Coverage report: `htmlcov/index.html`

**Called by:** Developer via terminal

---

## Best Practices

### For Backend Developers

1. **Always use scripts, never raw commands:**
   - ✅ `./scripts/local/run.sh`
   - ❌ `poetry run uvicorn ...`

2. **Test locally before CI:**
   - Run `./scripts/local/run-tests.sh` before pushing
   - Ensure unit and integration tests pass

3. **Keep scripts atomic:**
   - Each script does ONE thing
   - No orchestration logic in Tier 3 scripts

4. **Document environment variables:**
   - Update this document when adding new env vars
   - Use descriptive variable names

5. **Maintain backward compatibility:**
   - Scripts are called by Tier 2 orchestrators
   - Breaking changes require DevOps coordination

### For DevOps Engineers

1. **Call Tier 3 scripts from Tier 2:**
   - Never duplicate build/run logic in Tier 2
   - Use output from `build.sh` for image names

2. **Provide required environment variables:**
   - CI scripts expect infrastructure pre-provisioned
   - Pass connection strings via env vars

3. **Respect script ownership:**
   - Backend Developer owns Tier 3 scripts
   - DevOps owns Tier 2 orchestration

---

## Integration with Dockerfile

The `ci/run.sh` script is used as the container ENTRYPOINT:

```dockerfile
# Copy Tier 3 scripts
COPY scripts/ ./scripts/

# Make scripts executable
RUN chmod +x /app/scripts/ci/*.sh

# Use Tier 3 script as entrypoint
ENTRYPOINT ["/app/scripts/ci/run.sh"]
```

This ensures:
- Consistent startup behavior across environments
- Proper handling of `$PORT` for container runtime
- Centralized environment variable validation

---

## Troubleshooting

### Build script fails with "docker: command not found"
- Ensure Docker is installed and in PATH
- Build scripts must run from monorepo root

### Run script fails with missing environment variables
- Check `.env` file exists and is properly configured
- Verify ESO secrets are syncing in Kubernetes (production)

### Tests fail with "connection refused"
- Verify infrastructure is running (PostgreSQL, Dragonfly, NATS)
- For local tests, start Docker Compose or adjust connection strings

### Hot-reload not working in local development
- Ensure you're using `./scripts/local/run.sh` (not CI script)
- Check that Poetry is installed and dependencies are up-to-date

---

## Related Documentation

- **Script Hierarchy Standard:** `.claude/skills/standards/script-hierarchy-model.md`
- **Platform Documentation:** `../zerotouch-platform/README.md`
- **Service README:** `README.md`
- **Deployment Guide:** `services/agent_executor/DEPLOYMENT.md`

---

## Ownership

**Backend Developer:** Responsible for maintaining all Tier 3 scripts in this directory.

**Contact:** Ensure changes are tested locally and do not break CI/CD pipelines.
