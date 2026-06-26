# ADR-012: Middleware Stack Composition

**Date:** 2026-06-22
**Status:** Accepted

## Context

The harness-runtime uses LangChain's middleware system (`AgentMiddleware` subclasses) to inject behavior and tools into agents. Multiple middleware classes exist with different responsibilities, and their order in the stack affects behavior.

`create_agent()` and `create_deep_agent()` execute middleware hooks in a specific order:
- `before_*` hooks run in insertion order (first → last)
- `after_*` hooks run in reverse insertion order (last → first)
- `wrap_*` hooks nest like function calls (outermost wraps innermost)

Additionally, `create_deep_agent()` from `deepagents` has required middleware (`_REQUIRED_MIDDLEWARE`) that is always present: `FilesystemMiddleware` and `SubAgentMiddleware`. The harness-runtime adds additional middleware on top of these.

Without a documented ordering convention, middleware could conflict, tools could be missing, or hooks could fire in unexpected sequences.

## Decision

### Middleware stack order (first = outermost)

All topology builders and subagent builders use the same base order:

```
 1. TodoListMiddleware        — provides write_todos tool for task tracking
 2. FilesystemMiddleware      — provides read_file, write_file, ls, glob, grep, execute
  3. HumanInteractionMiddleware — provides ask_user and review_content HITL tools
 4. PatchToolCallsMiddleware  — repairs message history after interrupt/resume
 5. RubricMiddleware           — evaluates artifact quality after agent completes (optional)
 6. HumanInTheLoopMiddleware  — intercepts interrupt_on tools before execution (optional)
```

#### Rationale for ordering

1. **TodoList first**: Task tracking should be available to all subsequent middleware and the agent. No reason to put it later.

2. **Filesystem before HumanInteraction**: Filesystem tools are the most commonly used. Putting them early ensures they're in the base tool list before any conditional middleware.

3. **HumanInteraction after Filesystem**: User interaction tools should not interfere with filesystem tool auto-injection.

4. **PatchToolCalls after tools**: This middleware requires the full tool list (including middleware-provided tools) to be populated so it can properly correlate tool calls with tool results during resume.

5. **Rubric before HITL**: Rubric evaluation should run before human review — the human sees rubric-graded artifacts rather than raw agent output.

6. **HumanInTheLoop outermost (last)**: HITL middleware intercepts tool calls *before* they execute. Placing it last means it intercepts after all other middleware have processed the tool call. This ensures other middleware (Filesystem, HumanInteraction, PatchToolCalls) have already had their chance to modify or react to the tool call.

### Middleware injection points

| Topology | File | Where injected |
|---|---|---|
| Star (orchestrator) | `star_topology.py` | Appended to `middleware_stack` list, passed as `middleware` kwarg to `create_deep_agent()` |
| Acrylic (each node) | `node_compiler.py:build_node_middleware()` | Hardcoded list, passed as `middleware` to `create_agent()` |
| Subagents | `subagent_builder.py:_build_compiled_subagent()` | Appended to `middleware_stack`, passed as `middleware` to `create_agent()` |

`create_deep_agent()` also adds `_REQUIRED_MIDDLEWARE` (`FilesystemMiddleware`, `SubAgentMiddleware`) after the user-provided middleware list. The harness's own `FilesystemMiddleware` in the stack is redundant but harmless — `create_deep_agent` deduplicates by type.

### Adding a new middleware

To add a new middleware to the stack:

1. Create the middleware class in `core/` with a `.tools` list if it provides tools
2. Add it to the stack in all three injection points (star_topology, node_compiler, subagent_builder)
3. Document the intended position in the order above

If the middleware provides tools, it follows the builtin tool pattern (see ADR-010).

## Consequences

### Positive

- Consistent middleware behavior across all topologies
- Clear ordering rules prevent conflicts
- New middleware has a documented insertion protocol

### Negative

- `create_deep_agent`'s `_REQUIRED_MIDDLEWARE` adds `FilesystemMiddleware` again after the harness's own — redundant but harmless
- Changing the order requires touching three files
- Deep nesting of `wrap_*` hooks can make debugging challenging

## References

- `core/star_topology.py`: Middleware stack for star topology orchestrator
- `core/node_compiler.py`: `build_node_middleware()` for acrylic topology nodes
- `core/subagent_builder.py`: `_build_compiled_subagent()` for specialist subagents
- `deepagents/graph.py`: `_REQUIRED_MIDDLEWARE` definition (line ~205)
- `langchain/agents/factory.py`: `create_agent()` middleware collection logic (line ~894)
- ADR-010: Builtin Tool Architecture — middleware `.tools` injection pattern
- ADR-013: HITL / interrupt_on Protocol — HumanInTheLoopMiddleware placement rationale
