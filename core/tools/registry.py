"""In-process tool registry for the harness runtime.

Tools are loaded from the filesystem (``implementation.py``) at session startup
and registered here. The LLM runtime resolves tools by name from this registry
when executing agent turns.
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import BaseTool, tool


class ToolDef:
    """Container for a registered tool's metadata and callable."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        callable_: Callable,
        tool_def: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.callable = callable_
        self.tool_def = tool_def or {}

    def to_langchain_tool(self) -> BaseTool:
        """Convert to a LangChain ``BaseTool`` for use in the agent graph."""

        async def _run(**kwargs: Any) -> Any:
            return await self.callable(**kwargs)

        lc_tool = tool(_run)
        lc_tool.name = self.name
        lc_tool.description = self.description
        lc_tool.args_schema = self.parameters
        return lc_tool


class ToolRegistry:
    """Maps tool names to their definitions and callables.

    Usage::

        registry = ToolRegistry()
        registry.register("search", tool_def, handler)
        tool = registry.get("search")  # → ToolDef or None
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        name: str,
        tool_def: dict[str, Any],
        callable_: Callable,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")
        self._tools[name] = ToolDef(
            name=name,
            description=tool_def.get("description", ""),
            parameters=tool_def.get("parameters", {}),
            callable_=callable_,
            tool_def=tool_def,
        )

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def list_tools(self) -> dict[str, ToolDef]:
        return dict(self._tools)
