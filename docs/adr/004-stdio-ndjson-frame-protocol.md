# ADR-004: Stdio NDJSON Frame Protocol

**Date:** 2026-06-19
**Status:** Proposed

## Context

The Waypoint SDK spawns the harness-runtime as a stdio subprocess. They must communicate bidirectionally over stdin/stdout with a structured protocol that supports:

1. Session initialization with agent definition and input payload
2. Streaming text deltas from the LLM as they are generated
3. Full message frames (assistant responses, tool calls, tool results)
4. Session result with success/error/interrupted status
5. Human-in-the-loop resume (sending a resume payload after an interrupt)

The protocol must be line-delimited JSON (NDJSON) for simplicity of parsing on both sides. Each line is a complete JSON frame.

## Decision

### Frame Types

#### Incoming (SDK → Harness)

**`control_request`**: Initializes a session or triggers a control action.

```json
{
  "type": "control_request",
  "request_id": "req_1_abc123",
  "request": {
    "subtype": "initialize",
    "agent_definition": { "...": "..." },
    "input_payload": { "messages": [{"role": "user", "content": "..."}] },
    "sdk_mcp_servers": [{"name": "waypoint-platform", "transport": "stdio", ...}]
  }
}
```

Supported subtypes:
- `initialize` — Create or resume a session. If `resume_payload` is provided, resumes an interrupted session.
- `interrupt` — No-op control response (reserved).

**`user`**: Sends a user message (typically the orchestrator prompt or a HITL resume).

```json
{
  "type": "user",
  "message": { "role": "user", "content": "..." },
  "session_id": "sess_abc123"
}
```

#### Outgoing (Harness → SDK)

**`system`**: Session initialization metadata.

```json
{
  "type": "system",
  "subtype": "init",
  "session_id": "sess_abc123",
  "model": "deepseek-v4-flash",
  "tools": [{"name": "tool_1", "description": "..."}]
}
```

**`assistant`**: AI message with text content and/or tool calls.

```json
{
  "type": "assistant",
  "session_id": "sess_abc123",
  "message": {
    "role": "assistant",
    "content": [
      {"type": "text", "text": "Thinking..."},
      {"type": "tool_use", "id": "call_1", "name": "web_search", "input": {"q": "..."}}
    ]
  }
}
```

**`user`** (echo): Tool results echoed back to the SDK.

```json
{
  "type": "user",
  "session_id": "sess_abc123",
  "message": {
    "role": "user",
    "content": [
      {"type": "tool_result", "tool_use_id": "call_1", "content": "...", "is_error": false}
    ]
  }
}
```

**`stream_event`**: Text delta for real-time streaming.

```json
{
  "type": "stream_event",
  "session_id": "sess_abc123",
  "event": { "type": "text_delta", "text": "Hello, ", "index": 0 }
}
```

**`result`**: Session completion.

```json
{
  "type": "result",
  "session_id": "sess_abc123",
  "subtype": "success",
  "duration_ms": 1234,
  "num_turns": 1,
  "result": "Final output text",
  "structured_response": {"key": "value"},
  "files": {"file.txt": {"content": "..."}},
  "interrupt": {"action_requests": [...], "review_configs": [...]}
}
```

Result subtypes:
- `success` — Normal completion
- `interrupted` — Execution paused for HITL
- `error_during_execution` — Fatal error

**`control_response`**: Acknowledgement of a control request.

```json
{
  "type": "control_response",
  "response": {
    "subtype": "success",
    "request_id": "req_1_abc123",
    "session_id": "sess_abc123"
  }
}
```

### Protocol Flow

```
SDK                          Harness
  │                             │
  ├─ control_request(initialize)──►  Boot graph, load tools, connect MCP
  │◄── control_response          │
  │                              │
  ├─ user(prompt)───────────────►  Run full agent turn
  │◄── system(init)             │
  │◄── assistant(text+tool_use) │
  │◄── user(tool_result)        │
  │◄── assistant(text)          │
  │◄── result(success)          │
```

For interrupted sessions (HITL):
```
  │◄── result(interrupted)      │  Agent called interrupt()
  │                             │
  ├─ control_request(initialize  │  Resume with resume_payload
  │   + resume_payload)──────────►
  │◄── control_response         │
  │◄── assistant(...)           │
  │◄── result(success)          │
```

## Consequences

### Positive

- Stdio transport requires zero network configuration — the subprocess inherits stdin/stdout from the parent
- Line-delimited JSON is trivial to parse on both sides (Node.js `readline`, Python `sys.stdin.readline`)
- Each frame is self-contained with `session_id` for correlation

### Negative

- Stdout must be reserved exclusively for NDJSON; all logging goes to stderr via `structlog`
- Large tool results may cause line-length issues (mitigated by truncation)
- No multiplexing — one session per subprocess instance

## References

- `cli.py`: Main loop reading stdin, dispatching frame types
- `core/event_publisher.py`: `StdioPublisher` writes NDJSON to stdout
- `models/frames.py`: Frame dataclasses and serialization
- SDK `packages/waypoint-sdk/src/transition/plugins/ai-gateway/subprocess.ts`: `HarnessSubprocess` NDJSON reader/writer
