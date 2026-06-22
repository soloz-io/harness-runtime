# ADR-013: HITL / interrupt_on Protocol

**Date:** 2026-06-22
**Status:** Accepted

## Context

Some tool calls require human approval, editing, or response before execution. The harness-runtime supports this via LangGraph's interrupt mechanism and DeepAgents' `HumanInTheLoopMiddleware`.

Two distinct HITL patterns exist:
1. **Approval gate** (e.g., `script_reviewer`): Human approves, edits, or rejects a tool call. Rejection returns feedback to the agent.
2. **Ask user** (e.g., `ask_user`): Human provides unstructured text as the tool result. The tool body is never executed — the human's response IS the output.

These differ in semantics and `allowed_decisions`. Without a documented protocol, it's unclear which pattern applies when, and how the runtime should handle each.

## Decision

### Protocol: `interrupt_on` + `HumanInTheLoopMiddleware`

The `definition.json` orchestrator node declares which tools are interceptable via `interrupt_on`:

```json
{
  "interrupt_on": {
    "ask_user": {
      "allowed_decisions": ["approve", "edit", "reject", "respond"]
    }
  }
}
```

The topology builder passes this dict to `create_deep_agent(interrupt_on=...)` or wraps it in `HumanInTheLoopMiddleware(interrupt_on=...)` for acrylic nodes.

### The `respond` decision

The four `allowed_decisions` serve distinct purposes:

| Decision | Effect | When to use |
|---|---|---|
| `approve` | Execute tool with original args | Confirming a proposed action |
| `edit` | Modify tool args before execution | Correcting a proposed action |
| `reject` | Skip execution, return rejection feedback to agent | Denying a proposed action |
| **`respond`** | **Return the human's text as the tool result** | **Ask-user style tools** |

`respond` is unique: the tool body is **never executed**. The human's free-text input is injected directly as the `ToolMessage` content. This is essential for `ask_user` — the orchestrator needs the human's answer, not an approval/rejection.

### `ask_user` tool contract

The `ask_user` tool defines its schema for the LLM with three parameters:

```python
@tool("ask_user")
def ask_user(question: str, options: list[str] | None = None, blocking: bool = False) -> str:
    """Relay a question to the user and wait for their response."""
```

- `question`: The prompt presented to the human in the UI
- `options`: Optional predefined choices rendered as buttons
- `blocking`: Hint for the UI — `True` means the workflow cannot proceed without an answer

The tool body is a no-op. The `respond` decision ensures the human's text becomes the return value.

### Interrupt lifecycle

```
LLM calls ask_user(...)
  → HumanInTheLoopMiddleware intercepts (tool call is in interrupt_on)
  → Agent execution pauses
  → Runtime emits interrupt event to SDK/UI:
      { action_requests: [{ name: "ask_user", args: { question, options, blocking } }],
        review_configs: [{ action_name: "ask_user", allowed_decisions: ["respond"] }] }
  → Human submits response via UI
  → SDK sends Command(resume={ decisions: [{ type: "respond", message: "..." }] })
  → HumanInTheLoopMiddleware injects "..." as the ToolMessage content
  → Agent resumes with the response as the tool result
```

### `blocking` field semantics

The `blocking` field is a **hint for the UI** — it does NOT affect the runtime's interrupt behavior. Every `ask_user` call always halts and waits. The UI uses `blocking` to decide:
- `blocking: true` — Render as a modal that must be answered before proceeding
- `blocking: false` — Render as an inline prompt that can be deferred or dismissed

The orchestrator prompt uses `blocking` to decide whether the specialist can proceed with documented assumptions (non-blocking) or requires the answer before continuing (blocking).

### Runtime handling

1. **Star topology**: `create_deep_agent(interrupt_on=...)` — `HumanInTheLoopMiddleware` is auto-added to the main agent's middleware stack by `create_deep_agent`.
2. **Acrylic topology**: `build_node_middleware()` appends `HumanInTheLoopMiddleware(interrupt_on=...)` to the middleware stack when `interrupt_on` is present on the node config.
3. **Executor**: Detects `__interrupt__` in the stream output, extracts `action_requests` and `review_configs`, publishes an `interrupted` result frame. On resume, streams `Command(resume=resume_payload)` to the graph.

## Consequences

### Positive

- Clean separation between ask-user and approval-gate patterns
- `respond` decision eliminates the need for tool body execution
- UI can use `blocking` to choose the appropriate presentation
- Consistent lifecycle across topologies

### Negative

- The `blocking` field is advisory — the UI must respect it for it to matter
- `respond` on a non-ask-user tool would silently skip execution, which could be dangerous
- Four `allowed_decisions` creates a large configuration surface; misconfiguration can lead to unexpected behavior (e.g., allowing `approve` on `ask_user` would return the empty string from the placeholder body)

## References

- `core/star_topology.py`: `interrupt_on` passed to `create_deep_agent()`
- `core/node_compiler.py`: `HumanInTheLoopMiddleware(interrupt_on=...)` in `build_node_middleware()`
- `core/executor.py`: Interrupt detection and resume via `Command(resume=...)`
- `core/ask_user.py`: `ask_user` tool definition with `blocking` parameter
- `langchain/agents/middleware/human_in_the_loop.py`: `HumanInTheLoopMiddleware` implementation
- `deepagents/graph.py`: `create_deep_agent` — auto-adds `HumanInTheLoopMiddleware` when `interrupt_on` is provided
- ADR-010: Builtin Tool Architecture — middleware pattern for builtin tools
- ADR-012: Middleware Stack Composition — HumanInTheLoopMiddleware placement in the stack
