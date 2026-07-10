"""
Factory entry point for graph building.
"""

from typing import Any

from langchain_core.runnables import Runnable

from core.interfaces import TopologyBuilder
from core.tool_loader import load_tools_from_definition
from core.tool_registry import ToolRegistry
from core.topology.acrylic_topology import AcrylicTopologyBuilder
from core.topology.star_topology import StarTopologyBuilder


def build_agent_from_definition(
    definition: dict[str, Any],
    checkpointer: Any = None,
    tool_registry: ToolRegistry | None = None,
    *,
    workspace_id: str | None = None,
    session_id: str | None = None,
    db_pool: Any = None,
    backend: Any = None,
) -> Runnable[Any, Any]:
    """
    Build a complete LangGraph graph from an agent definition.

    Delegates to the appropriate topology builder (star or acrylic)
    based on the definition.
    """
    # 1. Resolve all tools from the ToolRegistry
    tool_definitions = definition.get("tool_definitions", [])
    available_tools = load_tools_from_definition(
        tool_definitions,
        registry=tool_registry,
    )

    # 2. Determine topology
    is_acrylic = False
    topology = definition.get("topology", "")
    if topology in ("custom", "acrylic"):
        is_acrylic = True
    elif topology == "agent-dag":
        is_acrylic = False
    else:
        edges = definition.get("edges", [])
        if any("condition" in edge or "conditions" in edge for edge in edges):
            is_acrylic = True

    # 3. Select strategy
    builder: TopologyBuilder
    if is_acrylic:
        builder = AcrylicTopologyBuilder()
    else:
        builder = StarTopologyBuilder()

    # 4. Build graph
    return builder.build(
        definition,
        available_tools,
        checkpointer,
        workspace_id=workspace_id,
        session_id=session_id,
        db_pool=db_pool,
        backend=backend,
    )
