# AGENTS.md — harness-runtime

## Project overview

Python 3.11+ project. LangGraph agent execution engine spawned as stdio subprocess by Waypoint SDK.
Communicates via LiteLLM NDJSON frame protocol over stdin/stdout.

## Quick commands

```bash
pip install -e ".[dev]"     # editable install with dev deps
black .                      # format (line-length 100)
ruff check .                 # lint
mypy .                       # typecheck (excludes tests/)
uv run ty core/              # stricter typecheck on core/ only
ruff check . && uv run ty core/  # full typecheck sequence
pytest                       # all tests (no -x by default, ~300s timeout)
```

## Test suites

- `pytest tests/test_protocol.py` — protocol contract tests (no DB needed, fast)
- `pytest tests/test_db_checkpoint.py` — integration tests (requires PostgreSQL, see `tests/docker-compose.yml` port 5433)
- `tests/run_db_tests.sh` — helper: starts Postgres via Docker Compose, runs DB tests, cleans up
- All tests via `conftest.py` in `tests/`

Quirks:
- `--strict-markers`, `--cov=.` in default pytest addopts; use `-k` for focused runs
- `test_db_checkpoint.py` is expensive (~300s timeout); expect slowness
- Mock LLM: set `USE_MOCK_LLM=true`, event replay from `tests/mock/` (submodule: `bizmatters/spec-engine`)

## Architecture

- **Entry point**: `cli.py:main` → registered as `harness-runtime` console script
- **Packages**: `core/` (business logic), `models/` (LiteLLM frame dataclasses), `cli.py` (CLI), `agent.py` (dev graph factory)
- **Two topology backends**: "start" (star topology, orchestrator+subagents) and "acrylic" (custom DAG with conditional edges)
- **Tool loading**: `core/tool_loader.py` uses `exec()` — definitions must come from trusted sources
- **Monkey-patch**: `core/structured_output.py` patches `langchain_openai` to inject DeepSeek `reasoning_content`
- **Session persistence**: LangGraph `PostgresSaver`, migrations in `migrations/` (LangGraph checkpoint tables)

## Required environment

- `DATABASE_URL` — PostgreSQL connection (required at runtime)
- `USE_MOCK_LLM=true` — skip real LLM calls (default `false`)
- `LLM_MODEL_NAME` — defaults to `gpt-4o-mini`
- `python-dotenv` loads `.env` if present

## Known quirks

- **Version mismatch**: `pyproject.toml` says `0.1.5`, `__init__.py` says `0.1.1`, egg-info says `0.1.3`
- **Old name remnants**: CI workflows, `workflows/` dirs, and some scripts still reference `deepagents-runtime`
- **No committed lockfile**: `uv.lock` in `.gitignore`
- **Submodule**: `tests/mock` → `bizmatters/spec-engine`; clone with `--recurse-submodules`
- **Python version mismatch**: local `.python-version` is `3.12.10`, Docker uses `3.11-slim`, mypy targets `3.11`
- **No pre-commit hooks, no Makefile/Justfile** — automation via `scripts/` shell scripts only
