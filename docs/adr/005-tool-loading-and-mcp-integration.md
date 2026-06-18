# ADR-005: Tool Loading and MCP Integration

**Date:** 2026-06-19
**Status:** Proposed

## Context

The harness-runtime needs to load tools from multiple sources:
1. **Script-based tools** — Python code embedded in `tool_definitions` (`runtime.script` field), executed via `exec()`
2. **MCP tools** — Remote tools exposed via the Model Context Protocol (stdio transport), loaded from SDK-provided MCP server definitions
3. **Extra tools** — Tools passed by the SDK at runtime (e.g., platform MCP servers for `ai-gateway/generate-text`)

Tool loading must be secure (or at least sandboxed), must resolve tool names to LangChain `BaseTool` instances, and must be available to both topology builders.

## Decision

### Tool Loading Strategy

1. **Script tools** are loaded first via `load_tools_from_definition()`. Each tool definition has a `runtime.script` field containing Python code. The code is `exec()`'d in an isolated namespace with `__builtins__`, and any `BaseTool` instance created is collected.

2. **MCP tools** are loaded asynchronously via `load_mcp_tools_from_servers()`. Each MCP server definition uses stdio transport — the harness spawns the MCP server process, connects via `langchain_mcp_adapters`, lists tools via `session.list_tools()`, and converts them to LangChain `BaseTool` instances.

3. Both tool sets are merged into a single `Dict[str, BaseTool]` and passed to the topology builder.

### Security Model

- `exec()` is used for script tool loading. This means any agent definition with `tool_definitions` containing `runtime.script` can execute arbitrary Python. This is acceptable because agent definitions come from trusted sources (the Waypoint platform, not end users).
- The execution namespace is intentionally not sandboxed beyond isolating local variables. Production deployments must validate definitions before loading.

### Tool Resolution

Both topology builders resolve tools by name:
- **StartTopologyBuilder**: Orchestrator node has `tools: ["tool_a", "tool_b"]` — the builder looks up each name in the available tools dict and passes the `BaseTool` instances to `create_deep_agent()`.
- **AcrylicTopologyBuilder**: Each node has `config.tools: ["tool_a"]` — `compile_node()` does the same lookup.

## Consequences

### Positive

- Flexible tool loading supports inline scripts, MCP servers, and SDK-provided tools
- Tool names in definitions are portable across topology builders
- MCP server handles are tracked for clean shutdown (`MCPServerHandle.cleanup()`)

### Negative

- `exec()` is inherently insecure — no real sandboxing. A malicious definition can compromise the harness process.
- MCP loading is async-only, requiring `asyncio.run()` in the synchronous `cli.py` main loop
- Only stdio MCP transport is supported (v1); SSE/HTTP is skipped with a warning

## References

- `core/tool_loader.py`: `load_tools_from_definition()`, `exec()`-based tool loading
- `core/mcp_loader.py`: `load_mcp_tools_from_servers()`, `MCPServerHandle`
- `core/factory.py`: Tool loading orchestration
- `core/node_compiler.py`: Tool resolution for acrylic topology nodes
- `core/start_topology.py`: Tool resolution for star topology orchestrator
