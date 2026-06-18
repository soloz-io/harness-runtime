# ADR-009: Per-Node Message Isolation in Acrylic Topology

**Date:** 2026-06-19
**Status:** Proposed

## Context

In the acrylic (custom DAG) topology, each node is an independent agent performing a specific task (e.g., topic research, script writing, human approval gate). These agents should not see each other's intermediate reasoning, tool calls, or tool results.

If message contexts were shared across nodes, the following problems arise:
- **Context window overflow**: Node B sees Node A's entire conversation, including tool results that may contain large files (search results, code diffs, etc.)
- **Cross-contamination**: Node B might react to artifacts left by Node A (tool call IDs, partial state) that are not relevant to its task
- **Security**: A downstream node could see data (API keys, internal decisions) that the upstream node should not share

The star topology does not have this problem because the orchestrator owns the message context — sub-agents communicate via the `task` tool, and their intermediate messages are sub-graph state that the orchestrator wraps into structured responses.

## Decision

### The `prep_agent` Pattern

Every node in the acrylic topology gets a `__prep_{node_id}__` node that resets messages to just the initial input before the agent runs:

```python
def prep_agent(state: dict[str, Any]) -> dict[str, Any]:
    init_msgs = state.get("__initial_messages", [])
    return {"messages": list(init_msgs)}
```

The initial messages are captured once at graph start by an `__init_messages__` node:

```python
def _capture_initial(state: dict[str, Any]) -> dict[str, Any]:
    return {"__initial_messages": list(state.get("messages", []))}
```

The graph structure is:

```
__init_messages__ → __prep_A__ → A → (conditional edge to) __prep_B__ → B → ...
```

Each `__prep_` node creates a fresh message context from the original input. Node B receives only the original user messages — not Node A's output or tool calls.

### Cross-Node Communication

Nodes communicate via **state fields**, not messages:
- Upstream nodes write to typed state fields defined in `definition["state_schema"]` (e.g., `state["proposed_changes"]`)
- Downstream nodes read from those fields
- Connection edges define the topology (`from → to` or conditional `source → [condition → target]`)

This is artifact-based communication, not conversation-based.

### Alternative Considered: Message Isolation via Sub-Graphs

An alternative approach is to compile each acrylic node as a sub-graph, which provides strong isolation but adds compilation and serialization overhead. The `prep_agent` approach is simpler and sufficient for the current use cases.

## Consequences

### Positive

- No context window contamination between nodes
- Each agent sees only the input prompt relevant to its task
- State fields provide a typed, explicit contract between nodes
- Simple implementation — no sub-graph compilation needed

### Negative

- Upstream nodes cannot pass conversational context downstream (if needed, they must write to state fields explicitly)
- The `__prep_` nodes add overhead (2 extra LangGraph steps per node)
- Initial messages are snapshotted once — if a node wants to see its own previous output, it must be explicitly passed through state

## References

- `core/acrylic_topology.py`: `_add_isolated_agent()`, `_capture_initial()`, `prep_agent()`
- `core/acrylic_topology.py`: `_build_state_schema()` — typed state fields for cross-node communication
