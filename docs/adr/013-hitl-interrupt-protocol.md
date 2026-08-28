# ADR-013: HITL / interrupt_on Protocol

**Date:** 2026-06-22
**Updated:** 2026-08-28
**Status:** Accepted (updated)

## Context

Some tool calls require human approval, editing, or response before execution. The harness-runtime supports this via LangGraph's interrupt mechanism.

Three distinct HITL patterns exist:
1. **Ask user** (e.g., `ask_user`): Human provides unstructured text as the tool result. The tool body calls `langgraph.types.interrupt()` directly — the graph pauses unconditionally, and the resume value IS the tool result.
2. **Phase review** (e.g., `review_content`): Human reviews a completed deliverable and either approves, rejects with feedback, or edits the content directly.
3. **Approval gate** (legacy `script_reviewer` pattern): Human approves, edits, or rejects a tool call before execution. Rejection returns feedback to the agent.

These differ in semantics and `allowed_decisions`. Without a documented protocol, it's unclear which pattern applies when, and how the runtime should handle each.

## Decision

### `ask_user` — Self-interrupting tool (updated 2026-08-28)

`ask_user` calls `langgraph.types.interrupt()` directly in its tool body. This means:

- **No `interrupt_on` config required** — the graph always pauses at a real LangGraph checkpoint interrupt when `ask_user` is invoked.
- **Checkpoint always has `__interrupt__`** — `Command(resume=resume_payload)` always works correctly to resume.
- The interrupt value is `{ name: "ask_user", args: { questions, type, file_path } }`, passed through `extract_interrupt_payload` and published as the `interrupt` field of the `ResultFrame`.
- On resume, LangGraph returns the `decisions` payload as the return value of `interrupt()`, which `ask_user` unpacks to extract the human's text as the `ToolMessage` content.

**Previous design (deprecated):** The tool body was a no-op and `HumanInTheLoopMiddleware` intercepted the call via `interrupt_on` config. This was fragile — forgetting `interrupt_on` caused the graph to run through `ask_user` without pausing, and sandbox restarts could leave the graph at `END` with no active interrupt, making `Command(resume=...)` a silent no-op.

### `review_content` and approval-gate tools — `interrupt_on` + `HumanInTheLoopMiddleware`

The `definition.json` orchestrator node declares which tools are interceptable via `interrupt_on`:

```json
{
  "interrupt_on": {
    "review_content": {
      "allowed_decisions": ["approve", "edit", "reject"]
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

For `ask_user`, the resume value from `interrupt()` is the decisions payload; the tool body extracts `decisions[0].message` and returns it as the tool result string.

### Builtin HITL tool contracts

Two builtin HITL tools are registered via `HumanInteractionMiddleware` (see ADR-010):

```python
@tool("ask_user")
def ask_user(
    questions: list[AskUserQuestion],
    type: Literal["approval", "clarification"] = "clarification",
) -> str:
    """Relay questions to the user and wait for their response."""
```

- `questions`: Array of question objects. Each item has `question` (str), optional `options` (list[str]), and optional `blocking` (bool).
- `type`: `"clarification"` for discovery questions the LLM asks naturally during research; `"approval"` when the LLM needs explicit go-ahead for a phase transition.

A `type: "clarification"` ask_user should use only `["respond"]` as the allowed decision. A `type: "approval"` ask_user may additionally allow `["approve", "edit", "reject"]` if the UI supports it.

```python
@tool("review_content")
def review_content(phase_name: str, content: str) -> str:
    """Request human review and approval of completed phase output."""
```

- `phase_name`: Human-readable label for the phase being reviewed.
- `content`: The deliverable content to present for review.

The tool body is a no-op. For `ask_user`, the `respond` decision ensures the human's text becomes the return value. For `review_content`, the `approve`/`reject`/`edit` decisions return the appropriate status and feedback.

### Interrupt lifecycle

Both tools follow the same interrupt lifecycle, differing only in `allowed_decisions`:

```
LLM calls ask_user(questions=[...], type="clarification")
  → HumanInTheLoopMiddleware intercepts (tool call is in interrupt_on)
  → Agent execution pauses
  → Runtime emits interrupt event to SDK/UI:
      { action_requests: [{ name: "ask_user", args: { questions: [...], type: "clarification" } }],
        review_configs: [{ action_name: "ask_user", allowed_decisions: ["respond"] }] }
  → Human submits response via UI
  → SDK sends Command(resume={ decisions: [{ type: "respond", message: "..." }] })
  → HumanInTheLoopMiddleware injects "..." as the ToolMessage content
  → Agent resumes with the response as the tool result
```

```
LLM calls review_content(phase_name="Script Review", content="...")
  → HumanInTheLoopMiddleware intercepts
  → Agent execution pauses
  → Runtime emits interrupt event:
      { action_requests: [{ name: "review_content", args: { phase_name: "Script Review", content: "..." } }],
        review_configs: [{ action_name: "review_content", allowed_decisions: ["approve", "edit", "reject"] }] }
  → Human reviews content, submits decision
  → SDK sends Command(resume={ decisions: [{ type: "approve" }] })
  → HumanInTheLoopMiddleware injects the decision as the ToolMessage content
  → Agent resumes
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
- `core/ask_user.py`: `ask_user` tool definition with `type` and `blocking` parameters
- `core/review_content.py`: `review_content` tool for phase output review
- `core/human_interaction.py`: `HumanInteractionMiddleware` — bundles both HITL tools into a single middleware
- `langchain/agents/middleware/human_in_the_loop.py`: `HumanInTheLoopMiddleware` implementation
- `deepagents/graph.py`: `create_deep_agent` — auto-adds `HumanInTheLoopMiddleware` when `interrupt_on` is provided
- ADR-010: Builtin Tool Architecture — middleware pattern for builtin tools
- ADR-012: Middleware Stack Composition — HumanInTheLoopMiddleware placement in the stack
