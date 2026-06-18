# ADR-003: Harness Runtime Architecture — Two Topology Builders

**Date:** 2026-06-19
**Status:** Proposed

## Context

The harness-runtime receives agent definitions from the Waypoint SDK via a stdio NDJSON frame protocol. Each definition describes one or more AI agents, their tools, and how they connect. The harness must build a LangGraph `CompiledStateGraph` from these definitions and execute it.

Agent definitions come in two distinct structural forms:
- **Star topology** (orchestrator + specialist sub-agents) — produced by the SDK's `buildAgentDefinition()` or provided manually as a simple definition
- **Custom DAG** with conditional edges and per-node message isolation — provided via `raw_agent_definition` with nodes, edges, tool_definitions, and conditional routing

These forms differ fundamentally in graph structure, execution semantics, and middleware requirements. A single builder cannot serve both without either overcomplicating the star path or constraining the DAG path.

## Decision

The harness-runtime uses **two topology builders**, selected auto-matically at graph build time:

```
factory.build_agent_from_definition(definition)
  │
  ├─ Explicit: definition["topology"] == "custom" | "acrylic"  → AcrylicTopologyBuilder
  │
  └─ Implicit: any edge has "condition" or "conditions" key    → AcrylicTopologyBuilder
       │
       └─ Neither                                                 → StartTopologyBuilder
```

### StartTopologyBuilder

**File:** `core/start_topology.py` — `StartTopologyBuilder`

- Calls `deepagents.create_deep_agent()` which internally delegates to `langchain.agents.create_agent()`
- Receives the **full deepagents middleware stack**: TodoListMiddleware, FilesystemMiddleware, SummarizationMiddleware, SubAgentMiddleware (for the built-in `task` delegation tool), PatchToolCallsMiddleware, MemoryMiddleware, SkillsMiddleware, HumanInTheLoopMiddleware, AnthropicPromptCachingMiddleware
- A single orchestrator node delegates to specialist sub-agents via the `task` tool
- Shared message context — the orchestrator sees all messages, including sub-agent tool calls and results
- No conditional edges; routing decisions happen inside the LLM's reasoning
- Sub-agents can be `SubAgent` dicts (declarative, compiled by deepagents) or `CompiledSubAgent` instances (pre-compiled runnables)

### AcrylicTopologyBuilder

**File:** `core/acrylic_topology.py` — `AcrylicTopologyBuilder`

- Builds a native LangGraph `StateGraph` from scratch, bypassing `create_deep_agent()`
- Each node is compiled individually via `compile_node()` → `create_agent()` (NOT `create_deep_agent()`)
- Each node receives only **essential middleware**: TodoListMiddleware, FilesystemMiddleware, PatchToolCallsMiddleware, optionally HumanInTheLoopMiddleware and StructuredOutputMappingMiddleware
- Per-node message isolation — a `__prep_` node resets each agent's messages to just the initial input before execution. Node B does not see Node A's tool calls or intermediate reasoning.
- Conditional edges via `eval()` expressions that read typed state fields (`approved`, `feedback`, `retry_count`)
- Budget/increment nodes for retry-count-based conditions
- Works with `DeepAgentState` or a dynamically-built subclass from `definition["state_schema"]`

## Rationale

1. **Separate middleware requirements**: Star topology needs SubAgentMiddleware and full deepagents tooling (skills, memory, summarization). Acrylic topology does not — each node is self-contained with isolated state.

2. **Different execution semantics**: Star topology has shared state and or-chestrator-driven delegation. Acrylic topology has isolated contexts and code-enforced routing.

3. **Conditional routing incompatibility**: The star topology's `create_deep_agent()` produces an opaque graph where conditional edges cannot be introspected or wired at the LangGraph level. Acrylic topology exposes explicit `add_conditional_edges()`.

4. **Auto-detection removes SDK coupling**: The SDK never needs to know which builder will be used. It sends a definition; the harness routes it correctly.

## Consequences

### Positive

- Each builder is focused and maintainable
- New topology builders can be added without changing existing ones or the SDK
- The auto-detection in `factory.py` is the single source of truth for topology routing
- Acrylic topology enables workflows that the star topology cannot express (e.g., human-in-the-loop routing based on typed approval fields)

### Negative

- Two code paths must be tested independently
- The auto-detection heuristic (edge conditions) is implicit — a definition author may not realize they triggered the acrylic path
- `builder.py` (the deprecated monolithic builder) must be maintained for backward compatibility until migration is complete

## References

- deepagents library: `create_deep_agent()` calls `create_agent()` after assembling full middleware stack
- `core/start_topology.py`: StartTopologyBuilder implementation
- `core/acrylic_topology.py`: AcrylicTopologyBuilder implementation
- `core/factory.py`: Auto-detection and routing logic
- `core/node_compiler.py`: Per-node compilation for acrylic topology
- ADR-004: Stdio NDJSON Frame Protocol
- ADR-005: Tool Loading and MCP Integration
