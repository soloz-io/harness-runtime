# ADR-011: Topology Builder Split — Star vs Acrylic

**Date:** 2026-06-22
**Status:** Accepted

## Context

Agent DAG definitions in Waypoint describe a graph of nodes (orchestrator + specialists) and edges. There are two fundamentally different graph structures:

1. **Star topology**: A single orchestrator node with specialist subagents. The orchestrator owns the message context and delegates work via the `task` tool. Specialists produce artifacts on the shared filesystem.
2. **Custom DAG / acrylic topology**: Multiple independent agent nodes connected by explicit edges with optional conditions. Each node has isolated message context. Nodes communicate through typed state fields.

A single topology builder handling both patterns would be complex, error-prone, and hard to maintain.

## Decision

### Two separate topology builders

The harness-runtime uses two topology builders, selected by `factory.py` based on the definition's topology field:

| Builder | Class | File | When used |
|---|---|---|---|
| **Star** | `StarTopologyBuilder` | `star_topology.py` | Default. `topology` is unset, `"start"`, or `"star"`. No conditional edges exist. |
| **Acrylic** | `AcrylicTopologyBuilder` | `acrylic_topology.py` | `topology` is `"custom"` or `"acrylic"`, OR any edge has `condition`/`conditions`. |

### Shared utilities

Both builders share:
- `subagent_builder.py`: Compiles specialist configs into `CompiledSubAgent` instances
- `node_compiler.py`: Compiles a single agent node from JSON config (used by acrylic for each node)
- `tool_loader.py`, `mcp_loader.py`: Tool loading from `tool_definitions` and MCP servers
- `rubric_middleware.py`: Rubric evaluation gates

### Star topology specifics

- Uses `create_deep_agent()` from `deepagents` package
- Orchestrator node has `interrupt_on` for HITL tools
- Specialists are `CompiledSubAgent` instances passed to `create_deep_agent(subagents=...)`
- No conditional edges — execution is orchestrator-driven
- Orchestrator's message context persists across all turns; specialist messages are sub-graph isolates

### Acrylic topology specifics

- Uses `create_agent()` from `langchain.agents` for each node
- Each node is compiled independently by `node_compiler.py`
- `__prep_` nodes reset message context per node (see ADR-009)
- Conditional edges (`state["field"] == "value"`) route between nodes
- Nodes communicate via typed state fields, not message history

## Consequences

### Positive

- Each builder has a clear, narrow responsibility
- Adding a new topology type is straightforward (create a new builder class)
- Star topology benefits from `deepagents` features (subagent lifecycle, filesystem middleware)
- Acrylic topology gives full control over DAG structure

### Negative

- Some logic is duplicated across builders (middleware injection, tool resolution)
- A definition that doesn't cleanly fit either topology may require effort to add a third builder
- The two paths have subtle behavioral differences (e.g., how `create_deep_agent` manages middleware vs raw `create_agent`)

## References

- `core/factory.py`: Selects topology builder based on definition
- `core/star_topology.py`: Star topology implementation
- `core/acrylic_topology.py`: Acrylic topology implementation
- `core/node_compiler.py`: Per-node compilation (used by acrylic)
- `core/subagent_builder.py`: Subagent compilation (used by star)
- ADR-009: Per-Node Message Isolation in Acrylic Topology
