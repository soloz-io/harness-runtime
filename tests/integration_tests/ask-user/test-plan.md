# Ask-User / Interrupt — Test Plan

See ADR-002 for test plan convention. See `test_ask_user.py` for implementation.

## Business Journeys

| ID | Journey | Assertion | Status |
|----|---------|-----------|--------|
| B1 | Agent with `allow_ask_user`, calls `ask_user` | `result {subtype:"interrupted", interrupt:{type:"ask_user", questions:[...], tool_call_id:"..."}}` | ✅ Passes |
| B2 | Interrupt shape is well-formed | Fields: `type`, `questions[{question,type,choices?,required?}]`, `tool_call_id` | ✅ Covered by B1 |
| B3 | Without `allow_ask_user` → no interrupt | `result {subtype:"success"}` with `interrupt: null` | ✅ Passes |
| B4 | Multiple consecutive `ask_user` calls | Each produces its own `interrupted` result | ⚠️ Flaky |

## Setup

- Real LLM (deepseek-v4-flash) via `DEEPSEEK_API_KEY` in `.env`
- Real PostgreSQL via `tests/docker-compose.yml` (port 5433)
- CLI subprocess (`cli.py`) — same code path as SDK consumers

## Known Issues

- B4 is flaky with deepseek-v4-flash: model is non-deterministic for tool calling and can exceed the 100s frame-read timeout. Not a code defect.
