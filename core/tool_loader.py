"""
Tool Loading Module for Agent Executor.

Resolves tools requested by the DAG definition against the in-process
``ToolRegistry``. Builtin tools are resolved natively by the LangGraph
runtime; ``app-tool`` tools must be registered in the ``ToolRegistry``
before use.
"""

from typing import Any

import structlog
from langchain_core.tools import BaseTool
from langchain_core.tools import tool as lc_tool

from core.tool_registry import ToolRegistry

logger = structlog.get_logger(__name__)

BUILTIN_TOOLS = {
    "read_file",
    "write_file",
    "ask_user",
    "compile_schema",
}


class ToolLoadingError(Exception):
    """Raised when a requested tool is not found in the registry."""

    pass


def load_tools_from_definition(
    tool_definitions: list[dict[str, Any]],
    registry: ToolRegistry | None = None,
) -> dict[str, BaseTool]:
    """
    Resolve tools requested by the DAG against the ToolRegistry.

    ``harness-builtin`` tools are resolved as pass-through stubs — the
    LangGraph runtime handles them natively via middleware.
    ``app-tool`` tools must be present in the registry (loaded earlier via
    ``embedded_tool_loader.load_tool_implementations()``).

    Args:
        tool_definitions: List of tool definition dicts from the agent DAG.
        registry: The ``ToolRegistry`` populated with loaded app tools.

    Returns:
        Dictionary mapping tool names to ``BaseTool`` instances.

    Raises:
        ToolLoadingError: If an ``app-tool`` is not found in the registry.
    """
    if not tool_definitions:
        logger.warning("no_tool_definitions_provided")
        return {}

    loaded_tools: dict[str, BaseTool] = {}

    for tool_def in tool_definitions:
        tool_name = tool_def.get("name")
        if not tool_name:
            logger.warning("tool_definition_missing_name")
            continue

        # Inline script via runtime.script
        runtime = tool_def.get("runtime", {})
        if isinstance(runtime, dict) and "script" in runtime:
            loaded_tools[tool_name] = _load_inline_script(tool_name, tool_def, runtime["script"])
            logger.info("tool_resolved_inline_script", tool_name=tool_name)
            continue

        kind = tool_def.get("kind", "app-tool")

        if kind == "harness-builtin":
            if tool_name not in BUILTIN_TOOLS:
                logger.warning(
                    "unknown_builtin_tool",
                    tool_name=tool_name,
                )
            loaded_tools[tool_name] = _builtin_stub(tool_name, tool_def)
            logger.info("tool_resolved_builtin", tool_name=tool_name)
            continue

        if kind != "app-tool":
            logger.warning("unknown_tool_kind_skipping", tool_name=tool_name, kind=kind)
            continue

        if registry is None:
            raise ToolLoadingError(
                f"Tool '{tool_name}' requires a ToolRegistry but none was provided"
            )

        entry = registry.get(tool_name)
        if entry is None:
            raise ToolLoadingError(
                f"Tool '{tool_name}' was not found in the ToolRegistry. "
                f"Available: {list(registry.list_tools().keys())}"
            )

        loaded_tools[tool_name] = entry.to_langchain_tool()
        logger.info("tool_resolved_from_registry", tool_name=tool_name)

    logger.info(
        "tools_loaded",
        total_tools=len(loaded_tools),
        tool_names=list(loaded_tools.keys()),
    )

    return loaded_tools


def _load_inline_script(name: str, tool_def: dict[str, Any], script: str) -> BaseTool:
    """Load a tool from an inline Python script (``runtime.script``).

    Executes the script in an isolated namespace and extracts the tool
    by name.  The script should use ``@tool`` from ``langchain_core.tools``
    to produce a ``BaseTool`` instance, or define a plain callable.
    """
    namespace: dict[str, Any] = {}
    try:
        exec(script, namespace)
    except Exception as e:
        raise ToolLoadingError(f"Failed to exec inline script for tool '{name}': {e}") from e

    tool_obj = namespace.get(name)
    if tool_obj is None:
        raise ToolLoadingError(
            f"Inline script for tool '{name}' did not define a symbol named '{name}'"
        )

    if isinstance(tool_obj, BaseTool):
        return tool_obj

    if callable(tool_obj):
        t = lc_tool(tool_obj)
        t.name = name
        t.description = tool_def.get("description", "")
        return t

    raise ToolLoadingError(
        f"Inline script for tool '{name}' defined '{name}' as "
        f"{type(tool_obj).__name__}, expected a callable or BaseTool"
    )


def _builtin_stub(name: str, tool_def: dict[str, Any]) -> BaseTool:
    """Create a minimal BaseTool stub for harness-builtin tools.

    These stubs have no implementation — the LangGraph runtime middleware
    intercepts them before execution.
    """
    from langchain_core.tools import tool as lc_tool

    async def _noop(**kwargs: Any) -> str:
        return f"builtin:{name}"

    t = lc_tool(_noop)
    t.name = name
    t.description = tool_def.get("description", "")
    return t
