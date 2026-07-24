---
name: harness-local-testing
description: >-
  Guide for running harness-runtime integration tests locally against a real
  PostgreSQL database and real LLM providers. ALWAYS use this skill when the
  user wants to run integration tests, debug test failures, set up a test
  database, troubleshoot port conflicts or DB connection issues during local
  testing, start/stop PostgreSQL for tests, check what env vars the tests
  need, or add a new integration test for the harness-runtime. Use ONLY for
  harness-runtime tests — not for SDK, BFF, frontend, or e2e testing.
---

# Harness-Runtime Local Testing

Guide for running `harness-runtime/` integration tests locally against a
real PostgreSQL database, real LLM providers, and the skills git repo.

## Prerequisites

- **Docker** — PostgreSQL container on port 5433 (via SDK's `tests/docker-compose.yml`)
- **Redis** — `redis-server` available on PATH (for `cli.py` event bus)
- **API keys + git credentials** — all in `harness-runtime/.env` (see table below)
- **Python 3.11+** — with dev dependencies installed (`pip install -e ".[dev]"`)

## Quick Start

```bash
cd harness-runtime
./scripts/test-setup.sh                                      # all tests
./scripts/test-setup.sh tests/integration_tests/skills/ -v   # specific tests
```

The script handles everything: validates `.env`, starts PostgreSQL + Redis,
initializes the `chat_messages` table, runs pytest, and cleans up on exit.

## Required Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `DEEPSEEK_API_KEY` | Yes | LLM provider key (deepseek-v4-flash). Used as the API key value. |
| `AI_GATEWAY_API_KEY` | Yes | LLM gateway key — set to the same value as `DEEPSEEK_API_KEY`. Required by `ModelFactory.create_model`. |
| `DATABASE_URL` | Yes | PostgreSQL connection (`postgresql://waypoint:waypoint@localhost:5433/waypoint_test`) |
| `AGENTREGISTRY_GIT_OWNER` | Yes | Git owner for skills repo clone (`soloz-io`) |
| `AGENTREGISTRY_GIT_REPO` | Yes | Git repo for skills clone (`agentregistry`) |
| `AGENTREGISTRY_GITHUB_TOKEN` | Yes | GitHub token used by `GitBackend` for auth |

All env vars are loaded from a single source: `harness-runtime/.env`.
The `test-setup.sh` script validates every variable is present.

### How env vars reach the server subprocess

The `sse_server` fixture starts `cli.py` as a subprocess with
`env={**os.environ, "PORT": "9876", ...}` — it inherits the full pytest
environment. Any env var exported before the fixture starts is available.
The `PORT=9876` env var is critical: without it, the server defaults to
port 3000 and the health check on port 9876 fails.

### Verification: git credentials

If the skills test returns 500 (now fixed to 400 with a clear message),
verify git credentials:

```bash
# Check what vars are loaded
echo "OWNER=$AGENTREGISTRY_GIT_OWNER REPO=$AGENTREGISTRY_GIT_REPO"

# Test git access
git ls-remote https://github.com/$AGENTREGISTRY_GIT_OWNER/$AGENTREGISTRY_GIT_REPO.git

# Check if the expected subfolder exists (default: packages/builders/src/skills)
git ls-tree -d HEAD packages/builders/src/skills
```

## Automated Setup Script

`scripts/test-setup.sh` manages the full lifecycle:

```bash
./scripts/test-setup.sh                                        # all tests
./scripts/test-setup.sh tests/integration_tests/skills/ -v     # skills tests
./scripts/test-setup.sh tests/integration_tests/sse/ -v        # SSE tests
```

The script:
1. Validates `harness-runtime/.env` exists (fails with per-variable guidance if missing)
2. Loads env vars from `harness-runtime/.env` (only if not already set)
3. Fails early if any required env var is missing
4. Kills any process on port 9876
5. Starts Redis if not already running
6. Starts PostgreSQL via SDK docker-compose (waits up to 30s)
7. Applies `tests/db_setup.sql` to create the `chat_messages` table
8. Traps EXIT to clean up (docker compose down)
9. Runs `uv run pytest` with the provided args

## Running Tests

Always use `test-setup.sh` — it validates env vars, starts infrastructure, and runs pytest:

```bash
# All integration tests
./scripts/test-setup.sh

# Specific test suite
./scripts/test-setup.sh tests/integration_tests/skills/ -v
./scripts/test-setup.sh tests/integration_tests/sse/ -v

# Single test function
./scripts/test-setup.sh tests/integration_tests/skills/test_skills_subagent.py::test_subagent_sees_skill_dir_not_container_root -v
```

Never run `pytest` directly against integration tests — the prerequisite checks and infrastructure lifecycle are in the script, not in pytest.

### Debugging: capturing server stdout/stderr

The `sse_server` fixture discards stdout and stderr (`subprocess.DEVNULL`).
To see server-side errors, start the server manually:

```bash
export PORT=9876
PYTHONPATH="." python cli.py 2>/tmp/harness-server.log &
SERVER_PID=$!
# ... run test against http://127.0.0.1:9876 ...
kill $SERVER_PID
cat /tmp/harness-server.log
```

## Test Infrastructure

### PostgreSQL

| Property | Value |
|----------|-------|
| Host | `localhost:5433` |
| User | `waypoint` |
| Password | `waypoint` |
| Database | `waypoint_test` |
| Start | `cd waypoint/packages/waypoint-sdk/tests && docker compose up -d postgres` |
| Stop | `cd waypoint/packages/waypoint-sdk/tests && docker compose down` |

The SDK's `tests/docker-compose.yml` provides PostgreSQL 16 with a `tmpfs`
data volume for fast test runs.

### Redis

| Property | Value |
|----------|-------|
| Host | `localhost:6379` |
| Start | `redis-server --daemonize yes --port 6379` |
| Check | `redis-cli ping` (should return `PONG`) |

Redis is required by `cli.py` for the event bus between the HTTP API and
SSE streams. Without Redis, the server will fail to start.

### Database Tables

The `tests/db_setup.sql` file creates the `chat_messages` table. This table
is normally owned by the SDK's Drizzle schema (`waypoint/packages/waypoint-sdk/src/db/schema.ts`),
but the harness-runtime's `message_writer` module writes to it. The test
setup creates it so integration tests can run without the SDK's full
migration pipeline.

### sse_server Fixture

Defined in `tests/integration_tests/conftest.py` (module-scoped):

```python
@pytest.fixture(scope="module")
def sse_server() -> None:
    cli_path = Path(__file__).parent.parent.parent / "cli.py"
    proc = subprocess.Popen(
        [sys.executable, str(cli_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PORT": "9876"},
    )
    for _ in range(200):
        if proc.poll() is not None:
            pytest.fail(...)
        try:
            resp = httpx.get(f"{BASE_URL}/health", timeout=2.0)
            if resp.status_code == 200: break
        except: pass
        time.sleep(0.1)
    yield
    proc.terminate()
```

The fixture inherits `os.environ` — all env vars from `.env` files must be
loaded at the module level before the fixture runs. The `sse_server` fixture
does NOT load `.env` files itself; the test module does that.

### read_sse_frames Helper

Defined in `tests/integration_tests/helpers.py`. Reads SSE frames from the
`GET /event?session_id=...` stream until a frame with `"type": "result"` is
found (added by `SSEEventPublisher.publish_result()`), then returns all
collected frames:

```python
def read_sse_frames(response, *, timeout_sec=120.0):
    for chunk in response.iter_bytes():
        # parse SSE data: lines, find "data: " prefix, JSON parse
        ...
        if frame.get("type") == "result":
            return frames
    return frames
```

## Test Architecture

### How integration tests work

All tests use the black-box HTTP pattern:

1. **`sse_server` fixture** starts a real uvicorn subprocess (port 9876)
2. Test defines agent definitions inline as Python dicts (or loads from `tests/mock/`)
3. Test POSTs to `POST /session/{id}/message` with agent definition + message
4. Test opens SSE stream to `GET /event?session_id={id}`
5. `read_sse_frames()` parses raw SSE bytes until a `result` frame
6. Test asserts on frame types, shapes, and business outcomes

### SSE Event Frame Format

The `SSEEventPublisher` wraps every event in a protocol envelope:

```json
{
    "type": "event",
    "event_id": "hex-uuid",
    "seq": 1,
    "method": "messages",
    "params": {
        "namespace": [],
        "timestamp": 1234567890,
        "data": {"event": "content-block-delta", "index": 0, "delta": {"text": "Hello"}}
    }
}
```

Assistant text is extracted from `method: "messages"` + `event: "content-block-delta"`
frames. The shared `assistant_text_from_frames()` helper in
`tests/integration_tests/helpers.py` does this:

```python
from tests.integration_tests.helpers import assistant_text_from_frames

assistant_text = assistant_text_from_frames(frames)
```
```

### Skills Integration Test

`tests/integration_tests/skills/test_skills_subagent.py` exercises:

```
HTTP server → Session._init_skills() → GitBackend → CompositeBackend
  → FilesystemBackend(virtual_mode=True) → create_deep_agent
  → SubAgent spec → SkillsMiddleware
```

Uses `tests/mock/definition.json` as the agent definition fixture with
`__PROMPT_*__` and `__INJECT_MODEL_NAME__` placeholders resolved inline.

The subagent is directed to list `/workspace/.builder/skills/` — this path
is the `FilesystemBackend` root_dir (from `GitBackend` clone + subfolder
navigation). With `virtual_mode=True`, the subagent sees only files within
this directory, NOT the container root.

### Key Business-Journey Assertions

| Label | Assertion | What it validates |
|-------|-----------|-------------------|
| K1 | `result["subtype"] == "success"` | Subagent delegation completes |
| K2 | `"skills"` appears in output | Subagent's SkillsMiddleware accessed the skills directory |
| K3 | No `/bin/` or `/etc/` in output | `virtual_mode=True` prevents container root leak |

## Troubleshooting

### POST returns 500 Internal Server Error

**Before the fix:** Unhandled exception in `Session.__init__`. The
`sessions.py:182-183` handler only caught `ValueError`. `GitBackendError`
(not a `ValueError`) propagated as 500.

**After the fix:** `GitBackendError` is caught alongside `ValueError` → 400
with the git error message in the response body.

If you see a 400 with a git error:

```bash
# Verify git credentials are loaded
echo "OWNER=$AGENTREGISTRY_GIT_OWNER REPO=$AGENTREGISTRY_GIT_REPO"

# Verify the token works
git ls-remote https://github.com/$AGENTREGISTRY_GIT_OWNER/$AGENTREGISTRY_GIT_REPO.git

# Check the token is in env
echo "TOKEN=${AGENTREGISTRY_GITHUB_TOKEN:+<set>}"
```

The `GitBackend` reads `AGENTREGISTRY_GITHUB_TOKEN` from `os.environ`.
In production (with Agent Vault proxy), the token is a placeholder that
the MITM proxy rewrites. Locally, the real token from `.env` is used.

### POST returns 400 Bad Request

Check the response body for the detail:

```python
r = httpx.post(...)
print(r.status_code, r.text)  # body has the error detail
```

Common causes:
- `workspace_id` not in POST body and `WORKSPACE_ID` env var not set
- Model config missing `model_name` in the first node's config
- `AGENTREGISTRY_GIT_OWNER` or `AGENTREGISTRY_GIT_REPO` not set

### Empty frames list (IndexError: list index out of range)

The POST may have returned an error (500 or 400). The `GET /event` returns
404 because no session was created. Check the POST response:

```python
r = httpx.post(...)
print(r.status_code, r.text)
r.raise_for_status()  # will show the actual error
```

### Server doesn't start on port 9876

The server defaults to port 3000. The `sse_server` fixture overrides with
`PORT=9876`. If starting manually:

```bash
export PORT=9876
PYTHONPATH="." python cli.py
```

### "relation chat_messages does not exist"

The `chat_messages` table was not created. Run the DB setup:

```bash
PGPASSWORD=waypoint psql -h localhost -p 5433 -U waypoint -d waypoint_test -f tests/db_setup.sql
```

This happens when running tests without the `test-setup.sh` script, or if
the PostgreSQL container was replaced (`docker compose down -v` clears data).

### K3 fail (subagent sees /bin/ or /etc/)

The `virtual_mode=True` fix in `core/session.py:114` may be missing:

```python
# core/session.py:114 — must have virtual_mode=True
fs_backend = FilesystemBackend(root_dir=str(gb.path), virtual_mode=True)
```

Without `virtual_mode=True`, `FilesystemBackend._resolve_path("/")` returns
filesystem root `/`, leaking the entire container filesystem to the subagent.

### Port 9876 already in use

```bash
lsof -i :9876
kill <PID>
```

### Redis not available

```bash
which redis-server
redis-server --daemonize yes --port 6379
redis-cli ping       # should return PONG
```

### PostgreSQL won't start or connect

```bash
pg_isready -h localhost -p 5433
cd waypoint/packages/waypoint-sdk/tests
docker compose down -v
docker compose up -d postgres
```

### DeepSeek API key missing

The test module calls `load_dotenv()` from `harness-runtime/.env` at import
time. If the key is missing, `ModelFactory.create_model` raises:

```
ValueError: AI_GATEWAY_API_KEY is not set
```

The skills test has a fallback: if `DEEPSEEK_API_KEY` is set but
`AI_GATEWAY_API_KEY` is not, it copies the value. But the SDK `.env`
has `AI_GATEWAY_API_KEY` directly. Ensure both `.env` files exist.

### Test times out

- Default timeout: 120s per test (from `pyproject.toml`)
- Increase: `--timeout 300` flag
- Check the LLM is responding (a slow API can cause timeouts)
- Check PostgreSQL responsiveness: `pg_isready -h localhost -p 5433`
- The test itself is fast (<30s) when everything works — timeouts usually
  indicate an infrastructure issue (PostgreSQL down, Redis down, or
  server process stderr discarded)

## File Reference

### Test Setup & Infrastructure

| File | Purpose |
|------|---------|
| `scripts/test-setup.sh` | Automated lifecycle: validate `.env` → start Redis → start PG → init DB → run pytest → cleanup |
| `tests/db_setup.sql` | Creates `chat_messages` table (owned by SDK, written by harness) |
| `tests/integration_tests/conftest.py` | `sse_server` fixture (module-scoped uvicorn subprocess on port 9876) |
| `tests/integration_tests/helpers.py` | `read_sse_frames`, `count_checkpoints`, `save_frames` |

### Application Code

| File | Purpose |
|------|---------|
| `cli.py` | HTTP server entry point (uvicorn + Redis event bus) |
| `core/session.py` | Session lifecycle, `_init_skills()` builds `CompositeBackend` with `FilesystemBackend(virtual_mode=True)` |
| `core/topology/star_topology.py` | Builds orchestrator + subagents from agent definition |
| `core/topology/subagent_builder.py` | Builds declarative `SubAgent` specs |
| `core/integration/git_backend.py` | Clones skills repo to temp directory (uses `AGENTREGISTRY_GITHUB_TOKEN` for local auth) |
| `api/routers/sessions.py` | HTTP handlers: POST message, GET event SSE stream (catches `GitBackendError` as 400) |
| `api/publisher.py` | `SSEEventPublisher`: writes protocol events to Redis; `publish_result()` emits `type: "result"` frame |

### Test Definitions

| File | Purpose |
|------|---------|
| `tests/integration_tests/skills/test_skills_subagent.py` | Skills integration test (K1/K2/K3 assertions) |
| `tests/mock/definition.json` | Full agent definition with orchestrator + 4 specialists |

### External Dependencies

| File | Purpose |
|------|---------|
| `waypoint/packages/waypoint-sdk/tests/docker-compose.yml` | PostgreSQL 16 container on port 5433 |
| `waypoint/packages/waypoint-sdk/.env` | Git credentials (`AGENTREGISTRY_GIT_*`) and API keys |
| `harness-runtime/.env` | `DEEPSEEK_API_KEY`, `DATABASE_URL` |

## Common Error Flow

```
POST /session/{id}/message → 500
  ├─ GitBackendError not caught → was 500 (now fixed → 400)
  ├─ PostgreSQL down → 500 (server startup fails)
  └─ Redis down → 500 (server startup fails)

GET /event?session_id={id} → 404
  └─ POST failed → no session created

frames[-1] → IndexError
  └─ POST failed → 0 frames returned
```
