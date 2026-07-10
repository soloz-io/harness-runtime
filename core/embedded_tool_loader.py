"""Embedded tool loader — discovers and loads tools from the filesystem.

Reads ``implementation.py`` from the pod filesystem (path specified in each
tool definition), imports it dynamically via ``importlib``, extracts the
``handler`` callable, and registers it in the ``ToolRegistry``.

No subprocess, no MCP protocol, no serialization — tools are native Python
functions loaded into the harness process.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import structlog

from core.tool_registry import ToolRegistry

logger = structlog.get_logger(__name__)

# Root of the workspace filesystem inside the sandbox pod
WORKSPACE_ROOT = Path("/workspace")


class ToolLoadingError(Exception):
    pass


def load_tool_implementations(
    tool_definitions: list[dict[str, Any]],
    registry: ToolRegistry,
) -> None:
    """Load ``app-tool`` implementations from the filesystem into *registry*.

    Skips tools with ``kind: "harness-builtin"`` — those are resolved
    natively by the LangGraph runtime middleware.

    For each ``kind: "app-tool"`` tool definition:
        1. Resolve the filesystem path from ``path`` field.
        2. Read ``implementation.py`` from that path.
        3. Dynamically import the module via ``importlib``.
        4. Extract the ``handler`` callable.
        5. Register in ``ToolRegistry``.

    Args:
        tool_definitions: List of tool definition dicts from the agent DAG.
        registry: The ``ToolRegistry`` to populate.

    Raises:
        ToolLoadingError: If a required ``implementation.py`` is missing or
            does not export a ``handler`` function.
    """
    if not tool_definitions:
        return

    for tool_def in tool_definitions:
        kind = tool_def.get("kind")
        name = tool_def.get("name", "unknown")

        if kind == "harness-builtin":
            continue

        if kind != "app-tool":
            logger.warning("unknown_tool_kind", tool_name=name, kind=kind)
            continue

        tool_path = tool_def.get("path")
        if not tool_path:
            raise ToolLoadingError(f"Tool '{name}' has kind 'app-tool' but no 'path' field")

        impl_path = _resolve_impl_path(tool_path)
        if not impl_path.exists():
            raise ToolLoadingError(f"implementation.py not found for tool '{name}' at {impl_path}")

        handler = _import_handler(name, impl_path)
        registry.register(name, tool_def, handler)
        logger.info("tool_loaded", tool_name=name, path=str(impl_path))

    loaded = registry.list_tools()
    logger.info(
        "app_tools_loaded",
        count=len(loaded),
        tool_names=list(loaded.keys()),
    )


def _resolve_impl_path(tool_path: str) -> Path:
    """Resolve the tool path to an absolute ``implementation.py`` path.

    The tool path in the definition is relative to ``/workspace/.spec/``,
    e.g. ``".spec/workflows/research/tools/TOOL-001/"``.
    """
    if tool_path.startswith("/workspace"):
        base = Path(tool_path)
    else:
        base = WORKSPACE_ROOT / tool_path.lstrip("/")
    return base / "implementation.py"


def _import_handler(name: str, path: Path) -> Any:
    """Dynamically import *path* and return its ``handler`` callable.

    The module must export a top-level ``handler`` — an async callable
    that accepts the tool parameters as keyword arguments and returns
    the tool result.
    """
    module_name = f"_tool_{name}"

    try:
        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise ToolLoadingError(f"Failed to create module spec for '{name}' at {path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        raise ToolLoadingError(f"Failed to load implementation.py for tool '{name}': {e}") from e

    handler = getattr(module, "handler", None)
    if handler is None:
        raise ToolLoadingError(
            f"Tool '{name}' implementation.py must export a top-level `handler` callable"
        )

    if not callable(handler):
        raise ToolLoadingError(f"Tool '{name}' exported `handler` is not callable")

    return handler
