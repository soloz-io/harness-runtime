# ADR-010: Builtin Tool Architecture — Middleware-Provided Tools

**Date:** 2026-06-22
**Status:** Accepted

## Context

The harness-runtime needs a set of foundational tools available to every agent regardless of the DAG definition: filesystem access (`read_file`, `write_file`), user interaction (`ask_user`, `review_content`), and task management (`write_todos`). These tools should not require a `tool_definitions` entry in every definition.json — they should be always present.

There are two mechanisms for making tools available to agents:
1. **`tool_definitions` pathway**: Define a Python script in the JSON, `exec()` it at load time, register the `BaseTool` in `available_tools`, and require each node's `tools` array to reference it.
2. **Middleware pathway**: Create an `AgentMiddleware` subclass with a `.tools` class attribute. `create_agent()` automatically collects `middleware.tools` and injects them into the agent node.

The `tool_definitions` pathway requires explicit wiring per-DAG and per-node. This is acceptable for domain-specific tools but burdensome for foundational ones.

## Decision

### Builtin tools use the middleware pathway

A builtin tool consists of two parts:

1. **`@tool("name")` function** — defines the tool's schema (name, description, parameters) for the LLM. If the tool is HITL-intercepted (see ADR-013), the body is a no-op.
2. **`AgentMiddleware` subclass** with `tools = [tool_function]` — injects the tool into any agent that includes this middleware.

```python
@tool("ask_user")
def ask_user(
    questions: list[AskUserQuestion],
    type: Literal["approval", "clarification"] = "clarification",
) -> str:
    """Relay questions to the user and wait for their response."""
    return ""  # No-op — intercepted by HumanInTheLoopMiddleware

@tool("review_content")
def review_content(phase_name: str, content: str) -> str:
    """Request human review and approval of completed phase output."""
    return ""  # No-op — intercepted by HumanInTheLoopMiddleware

class HumanInteractionMiddleware(AgentMiddleware):
    tools = [ask_user, review_content]
```

### Middleware is added to all topology builders

Every topology builder (star, acrylic) and every subagent builder includes the same set of builtin middleware classes in the middleware stack. This ensures a consistent tool surface across all agent types.

### No `tool_definitions` entry needed

Builtin tools are NOT listed in `tool_definitions`. They appear in `definition.json` `tools` arrays only for documentation/clarity — the middleware injects them regardless. The runtime's tool resolution loop (which iterates `tools` and looks up names in `available_tools`) simply ignores names not found.

### Registration in `builtin-tools.ts` (TypeScript SDK)

For the AI Gateway path (which dynamically builds agent definitions via `agent-builder.ts`), builtin tools are also registered in `packages/waypoint-sdk/.../builtin-tools.ts` with their Python source. This allows `agent-builder.ts` to resolve builtin tool names to `tool_definitions` entries when building definitions that don't go through the middleware pathway.

## Consequences

### Positive

- Builtin tools are available everywhere without per-DAG wiring
- Adding a new builtin means: write the tool + middleware, wire into 3 topology builders
- Consistent tool surface across star and acrylic topologies
- No duplication across DAG definitions

### Negative

- Two tool pathways can cause confusion — developers must know whether a tool is middleware-provided or needs a `tool_definitions` entry
- Middleware order matters (see ADR-012) — tools injected too early/late in the stack can behave differently
- Tools that are only in `builtin-tools.ts` but not in a middleware will fail at runtime in the spec-engine path (which relies on the middleware)

## References

- `core/ask_user.py`: `ask_user` tool — canonical builtin example (HITL interceptable)
- `core/review_content.py`: `review_content` tool — phase review builtin (HITL interceptable)
- `core/human_interaction.py`: `HumanInteractionMiddleware` — bundles all HITL builtin tools
- `deepagents/middleware/filesystem.py`: `FilesystemMiddleware` — the original middleware-provided tool pattern
- `deepagents/middleware/todo.py`: `TodoListMiddleware` — provides `write_todos` tool
- `core/star_topology.py`, `core/node_compiler.py`, `core/subagent_builder.py`: Middleware injection points
- `packages/waypoint-sdk/.../builtin-tools.ts`: TypeScript builtin registry for AI Gateway path
- ADR-005: Tool Loading and MCP Integration — the `tool_definitions` pathway
- ADR-012: Middleware Stack Composition — middleware ordering rules
- ADR-013: HITL / interrupt_on Protocol — how interceptable tools work
