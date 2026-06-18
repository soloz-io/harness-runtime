 MCP Server Loading (sdk_mcp_servers)
#	Test	SDK expectation
D1	SDK sends initialize with sdk_mcp_servers: [{name:"waypoint-platform", transport:"stdio", command:"node", args:["...","--mcp-server","ai-gateway/generate-text"]}] → harness spawns the MCP server, loads its tools, includes them in the agent's toolset	Platform MCP tools are available to the LLM
D2	sdk_mcp_servers: [] (empty) → harness runs without MCP, no tools loaded	Backward-compatible with no MCP
D3	sdk_mcp_servers omitted entirely from initialize → same as empty	Backward-compatible
D4	MCP server spawn fails (bad command, timeout) → harness logs warning, runs without MCP tools, does NOT fail the session	One bad server never blocks the session
D5	MCP server tool calls work end-to-end: agent calls an MCP tool, harness calls the server, result flows back as a tool result	MCP tools work as expected