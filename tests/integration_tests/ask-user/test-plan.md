# HITL Gate Tool / Interrupt — Test Plan

See ADR-002 for test plan convention. See `test_ask_user.py` for implementation.

## Business Journeys

| ID | Journey | Assertion | Status |
|----|---------|-----------|--------|
| H1 | Agent with `interrupt_on`, calls gate tool | `result {subtype:"interrupted", interrupt:{action_requests:[...], review_configs:[...]}}` | ✅ Passes |
| H2 | Interrupt shape is well-formed | Fields: `action_requests[{name, args, description?}]`, `review_configs[{action_name, allowed_decisions}]` | ✅ Covered by H1 |
| H3 | Without `interrupt_on` -> no interrupt | `result {subtype:"success"}` with `interrupt: null` | ✅ Passes |
| H4 | Multiple consecutive gate tool calls | Each produces its own `interrupted` result | ⚠️ Flaky |

## Setup

- Real LLM (deepseek-v4-flash) via `DEEPSEEK_API_KEY` in `.env`
- Real PostgreSQL via `tests/docker-compose.yml` (port 5433)
- CLI subprocess (`cli.py`) — same code path as SDK consumers

## Known Issues

- H4 is flaky with deepseek-v4-flash: model is non-deterministic for tool calling and can exceed the 100s frame-read timeout. Not a code defect.
