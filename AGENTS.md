# AGENTS.md — harness-runtime

## Project overview

Python 3.11+ LangGraph agent execution engine, spawned as stdio subprocess by Waypoint SDK.
Communicates via LiteLLM NDJSON frame protocol over stdin/stdout. Also has HTTP server mode (FastAPI + uvicorn).

## Quick commands

```bash
pip install -e ".[dev]"          # editable install with dev deps
ruff check .                     # lint
ruff format --check .            # format check
uv run ty core/                  # typecheck core/ only (strict)
ruff check . && uv run ty core/  # full pre-commit check sequence
pytest                           # all tests (~300s timeout, needs DB)
pytest tests/test_artifact_reader.py  # run a single test file
pytest -k "test_name"            # run matching test(s)
```

Formatting uses `ruff format` (line-length 100, target py311). `black` is listed in dev deps but **not** used by pre-commit — do not run `black .`.

## Pre-commit hooks

```bash
pip install pre-commit && pre-commit install   # one-time setup
pre-commit run --all-files                      # manual run
```

Three hooks run on every commit:
1. `ruff check .` — lint
2. `ruff format --check .` — format check
3. `uv run ty check core/` — typecheck

If any fails, the commit is aborted. Skip with: `git commit --no-verify`.

## Test suites

Test files are in `tests/` (unit) and `tests/integration_tests/` (integration).

```bash
# Unit tests (no DB needed)
pytest tests/test_artifact_reader.py tests/test_artifact_backend.py

# Integration tests (requires PostgreSQL + Redis — use the setup script)
./scripts/test-setup.sh                                          # all integration tests
./scripts/test-setup.sh tests/integration_tests/sse/ -v          # focused run
./scripts/test-setup.sh tests/integration_tests/checkpointer/ -v # checkpointer tests
```

Integration test infrastructure (Postgres on port 5433, Redis on port 6379) is managed by `scripts/test-setup.sh`, which loads `.env` and tears down containers on exit.

Quirks:
- Default pytest addopts include `--strict-markers`, `--cov=.`, `--timeout=300`; use `-k` for focused runs
- `asyncio_mode = "auto"` — no need for `@pytest.mark.asyncio` on async tests
- Mock LLM: set `USE_MOCK_LLM=true` for event replay from `tests/mock/` (submodule: `bizmatters/spec-engine`)

## Architecture

- **Entry point**: `cli.py:main` → `harness-runtime` console script
- **HTTP server**: `api/` package (FastAPI + SSE), started via uvicorn
- **Packages**: `core/` (business logic), `models/` (LiteLLM frame dataclasses), `api/` (HTTP layer), `migrations/` (DB schema)
- **Two topology backends**: "start" (star topology, orchestrator+subagents) and "acrylic" (custom DAG with conditional edges)
- **Tool loading**: `core/tool_loader.py` uses `exec()` — definitions must come from trusted sources
- **Monkey-patch**: `core/structured_output.py` patches `langchain_openai` to inject DeepSeek `reasoning_content`
- **Session persistence**: LangGraph `PostgresSaver`, migrations in `migrations/`

## Required environment

`.env` is gitignored. Required vars for integration tests and runtime:

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection | — |
| `HARNESS_IMAGE_DIR` | Path to agents/ root (no git-clone fallback) | — |
| `AI_GATEWAY_API_KEY` | LLM gateway key | — |
| `USE_MOCK_LLM` | Skip real LLM calls | `false` |
| `LLM_MODEL_NAME` | Model name | `gpt-4o-mini` |

`python-dotenv` loads `.env` automatically from project root.

## Skills sourcing

`SkillsManager` (`core/session/skills.py`) resolves per-node skills from:
```
{HARNESS_IMAGE_DIR}/{node-id}/skills/
```
`HARNESS_IMAGE_DIR` is required — there is no git-clone fallback.
`HARNESS_SKILLS_RUNTIME_BASE` (default `/workspace/.builder`) relocates where skills are exposed for testing outside the container.

## CI / Docker

- Dockerfile: `python:3.11-slim`, installs redis-server, entrypoint at `scripts/ci/run.sh`
- CI scripts: `scripts/ci/` (build, run, run-migrations, in-cluster-test)
- No GitHub Actions workflows found in repo (CI may live elsewhere or be triggered externally)

## Known gotchas

- **Version mismatch**: `pyproject.toml` says `0.1.13`, `__init__.py` says `0.1.1` — do not trust either for the "real" version
- **Old name remnants**: some `.gitignore` entries and egg-info dirs still reference `deepagents-runtime`
- **No committed lockfile**: `uv.lock` is gitignored
- **Submodule**: `tests/mock` → `bizmatters/spec-engine`; clone with `--recurse-submodules`
- **Python version**: local `.python-version` is `3.12.10`, Docker uses `3.11-slim`, mypy/ty target `3.11`
- **Redis required**: integration tests and the HTTP event bus need Redis on port 6379
- **Port conflicts**: `scripts/test-setup.sh` kills processes on port 9876 before starting; Postgres is on 5433
