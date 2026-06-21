# SSE Event Pipeline — Test Plan

See ADR-002 for test plan convention. See `test_sse_pipeline.py` for implementation.

## Business Journeys

| ID | Journey | Assertion | Status |
|----|---------|-----------|--------|
| S1 | POST message → SSE delivers all frame types in order | `connected` → `system_init` → `stream_event*` → `assistant` → `result` | 🟡 Not implemented |
| S2 | Publisher uses correct session ID in Redis stream key | Redis key is `session:{session_id}:events` (not `_init_`) | 🟡 Not implemented |
| S3 | Two concurrent SSE consumers receive the same events (multi-device fan-out) | Both streams receive identical frames (same types, same order) | 🟡 Not implemented |
| S4 | SSE stream terminates cleanly after `result` frame | Sentinel written, consumer reads EOF after sentinel | 🟡 Not implemented |

## Setup

- Real LLM (deepseek-v4-flash) via `DEEPSEEK_API_KEY` in `.env` (same as checkpointer tests)
- Real PostgreSQL via `tests/docker-compose.yml` (port 5433)
- CLI subprocess (`cli.py`) started in HTTP server mode — uvicorn on port 3000
- `httpx` for HTTP SSE streaming (already available in venv)
- Concurrent SSE connections use Python `threading` or `asyncio`
- Redis verification via `redis-cli` (installed inside harness Docker or via `docker exec`)

## Known Issues

- S3 (multi-device fan-out) requires two SSE consumers for the same session ID. The harness HTTP API supports this, but the test must coordinate timing so both consumers are connected before the POST completes.
- S2 (Redis key verification) is an internal implementation detail — the SDK never checks Redis. Included to explicitly verify the publisher bug fix.
- Real LLM calls are non-deterministic. The test retries frame reads with a timeout (matching the existing `read_turn_fast` pattern) and may fail if the model takes too long.
