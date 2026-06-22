"""MCP tool loader — connects to MCP servers and loads tools dynamically.

Discovers MCP servers from agent definition config, establishes SSE or
stdio connections, and loads available tools as LangChain BaseTool instances.
"""

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import structlog
from langchain_core.tools import BaseTool

logger = structlog.get_logger(__name__)


class MCPLoaderError(Exception):
    pass


class MCPServerHandle:
    def __init__(self, name: str, session: Any, exit_stack: AsyncExitStack) -> None:
        self.name = name
        self.session = session
        self.exit_stack = exit_stack

    async def cleanup(self) -> None:
        try:
            await asyncio.wait_for(self.exit_stack.aclose(), timeout=5.0)
        except TimeoutError:
            logger.warning("mcp_server_cleanup_timeout", server=self.name)
        except Exception:
            logger.warning("mcp_server_cleanup_failed", server=self.name, exc_info=True)


async def load_mcp_tools_from_servers(
    servers: list[dict[str, Any]],
) -> tuple[dict[str, BaseTool], list[MCPServerHandle]]:
    """Load MCP tools from configured MCP server definitions.

    Only stdio transport is supported in v1. SSE/HTTP transports are
    explicitly skipped with a warning and are planned for v2.
    """
    if not servers:
        return {}, []

    try:
        from langchain_mcp_adapters.sessions import StdioConnection, create_session
        from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
    except ImportError as e:
        logger.error(
            "langchain_mcp_adapters not installed. Install with: pip install langchain-mcp-adapters"
        )
        raise MCPLoaderError(
            "langchain-mcp-adapters package is required for MCP tool loading"
        ) from e

    tools: dict[str, BaseTool] = {}
    handles: list[MCPServerHandle] = []

    for server in servers:
        name = server.get("name", "unknown")
        transport = server.get("transport", "stdio")
        command = server.get("command")
        args = server.get("args", [])

        if transport != "stdio":
            logger.warning("mcp_server_unsupported_transport", server=name, transport=transport)
            continue

        if not command:
            logger.warning("mcp_server_missing_command", server=name)
            continue

        try:
            exit_stack = AsyncExitStack()
            session: Any = None

            try:
                connection: StdioConnection = {
                    "transport": "stdio",
                    "command": command,
                    "args": args,
                }
                session = await exit_stack.enter_async_context(create_session(connection))
                await session.initialize()
            except Exception:
                await exit_stack.aclose()
                raise

            result = await session.list_tools()

            server_tools = 0
            for mcp_tool in result.tools:
                lc_tool = convert_mcp_tool_to_langchain_tool(mcp_tool)
                lc_tool.name = mcp_tool.name
                tools[mcp_tool.name] = lc_tool
                server_tools += 1

            handle = MCPServerHandle(name=name, session=session, exit_stack=exit_stack)
            handles.append(handle)
            logger.info("mcp_server_loaded", server=name, tool_count=server_tools)

        except Exception as e:
            logger.error("mcp_server_load_failed", server=name, error=str(e))

    return tools, handles
