# ADR-006: Session Persistence and Checkpointing

**Date:** 2026-06-19
**Status:** Proposed

## Context

AI agent execution can be long-running (minutes to hours). The harness must persist execution state to survive process restarts and support human-in-the-loop suspension/resumption. Additionally, interrupted sessions must be resumable — the agent's state at the point of interruption must be fully recoverable.

LangGraph provides a built-in checkpointing mechanism via `BaseCheckpointSaver`. The harness uses `PostgresSaver` for production persistence.

## Decision

### Checkpointing Backend

**PostgreSQL** via `langgraph.checkpoint.postgres.PostgresSaver`. The connection string comes from the `DATABASE_URL` environment variable.

```python
ctx = PostgresSaver.from_conn_string(postgres_connection_string)
saver = ctx.__enter__()
saver.setup()  # creates tables if not present
```

### Session Lifecycle

1. **Session creation**: When the SDK sends a `control_request(initialize)`, the `Session` object generates a `session_id` (format: `sess_{24 hex chars}`). The agent graph is compiled with the `PostgresSaver` as checkpointer, keyed by `thread_id = session_id`.

2. **Turn execution**: `ExecutionManager.execute()` runs the compiled graph with `RunnableConfig(configurable={"thread_id": session_id})`. LangGraph's `PostgresSaver` automatically persists state after each Pregel super-step.

3. **Interruption**: When a node calls `interrupt()` (from `langgraph.types`), LangGraph suspends execution and persists the full state. The executor catches the `__interrupt__` in the stream output and publishes a `result(interrupted)` frame with the interrupt payload.

4. **Resumption**: The SDK sends a new `control_request(initialize)` with `resume_payload` and the same `session_id`. The harness creates a `Session` with `existing_session_id`, and the executor streams `Command(resume=resume_payload)` to the graph, which resumes from the last checkpoint.

### Migration Management

Database migrations for checkpoint tables live in `migrations/`. These are standard LangGraph checkpoint migrations and should be applied before starting the harness.

### Turn Counting

The `Session` object tracks `self.turns` (incremented on each `run_turn()` or `resume_turn()` call). This is reported in the `result` frame as `num_turns` for observability.

## Consequences

### Positive

- Full session persistence — the harness can crash and resume without losing state
- HITL suspension/resumption works across process restarts
- Standard LangGraph checkpointing requires no custom persistence code

### Negative

- Requires a running PostgreSQL instance at harness startup
- Checkpoint I/O overhead — every Pregel step writes to the database
- LangGraph checkpoint tables are not part of the platform's application schema (they're in a separate namespace)

## References

- `core/session.py`: `Session` class, session_id generation, turn tracking
- `core/executor.py`: `ExecutionManager.execute()`, interrupt detection, resume via `Command(resume=...)`
- `cli.py`: Main loop — creates `ExecutionManager` with `PostgresSaver`, handles `resume_payload` in initialize
- `migrations/`: LangGraph checkpoint table migrations
