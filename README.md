# deepagnets-runtime

Event-driven Python service for secure and stateful execution of LangGraph agents in Kubernetes with KEDA autoscaling.

## Overview

- **Event Processing**: NATS JetStream + KEDA autoscaling (1-10 pods)
- **State Persistence**: PostgreSQL + LangGraph Checkpointer
- **Real-time Streaming**: Dragonfly (Redis-compatible)
- **Secret Management**: External Secrets Operator (ESO) syncs from AWS Parameter Store
- **Database Provisioning**: Crossplane auto-generates PostgreSQL and Dragonfly
- **Deployment**: GitOps via ArgoCD (no manual kubectl)

## Quick Start

### Local Development

```bash
# Install dependencies
uv sync --all-extras

# Configure environment
cp .env.example .env
# Edit .env with your local credentials

# Run service
uv run uvicorn api.main:app --reload --port 8080

# Run tests
uv run pytest
```

### Production Deployment (GitOps)

```bash
# 1. Update claims in platform/claims/intelligence-deepagents/
# 2. Commit and push to Git
git add platform/claims/
git commit -m "feat: update deepagnets-runtime"
git push origin main

# 3. ArgoCD syncs automatically
# 4. Verify deployment
kubectl get deployment deepagnets-runtime -n intelligence-deepagents
kubectl get scaledobject -n intelligence-deepagents
```

## Architecture

```
NATS JetStream → NATS Consumer → Parse CloudEvent → Build Graph → Execute → Emit Result
                                                          ↓
                                                    PostgreSQL (checkpoints)
                                                    Dragonfly (streaming)
```

**Namespace**: `intelligence-deepagents`  
**Scaling**: 1-10 pods based on NATS queue depth  
**Resources**: 500m-2000m CPU, 1-4Gi memory per pod

## Configuration

### Environment Variables (Production)

Secrets auto-injected via ESO and Crossplane:

- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` - From ESO (AWS Parameter Store)
- `PG_HOST`, `PG_PORT`, `PG_USER`, `PG_PASSWORD`, `PG_DATABASE` - From Crossplane secret
- `REDIS_HOST`, `REDIS_PORT` - From Crossplane secret
- `NATS_URL`, `NATS_STREAM_NAME`, `NATS_CONSUMER_GROUP` - From EventDrivenService

### Secret Management

**LLM API Keys** (via ESO):
```bash
# AWS SSM Parameters
/zerotouch/prod/agent-executor/openai_api_key
/zerotouch/prod/agent-executor/anthropic_api_key

# ESO syncs to Kubernetes Secret: agent-executor-llm-keys
```

**Database Credentials** (via Crossplane):
```bash
agent-executor-db-conn        # PostgreSQL (auto-generated)
agent-executor-cache-conn     # Dragonfly (auto-generated)
```

## Platform Integration

### Claims Structure

```
platform/claims/intelligence-deepagents/
├── postgres-claim.yaml              # PostgreSQL database
├── dragonfly-claim.yaml             # Dragonfly cache
├── external-secrets/
│   └── llm-keys-es.yaml            # LLM API keys (ESO)
└── agent-executor-deployment.yaml   # EventDrivenService claim
```

### KEDA Autoscaling

- **Trigger**: NATS JetStream consumer lag
- **Threshold**: 5 messages per pod
- **Min/Max**: 1-10 pods

## Development

```bash
# Code quality
uv run black .
uv run ruff check .
uv run mypy .

# Testing
uv run pytest tests/unit/          # Unit tests
uv run pytest tests/integration/   # Integration tests
uv run pytest --cov                # With coverage
```

## References

- [Platform Architecture](../zerotouch-platform/README.md)
- [External Secrets Operator](https://external-secrets.io/)
- [Crossplane](https://docs.crossplane.io/)
- [KEDA](https://keda.sh/)
- [LangGraph](https://python.langchain.com/docs/langgraph)
