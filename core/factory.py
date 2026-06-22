"""
Factory entry point for graph building.
"""

from typing import Any, Dict, Optional

from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from core.acrylic_topology import AcrylicTopologyBuilder
from core.interfaces import TopologyBuilder
from core.star_topology import StarTopologyBuilder
from core.tool_loader import load_tools_from_definition


def build_agent_from_definition(
    definition: Dict[str, Any],
    checkpointer: Any = None,
    extra_tools: Optional[Dict[str, BaseTool]] = None,
) -> Runnable[Any, Any]:
    """
    Build a complete LangGraph graph from an agent definition.

    Delegates to the appropriate topology builder (start or acrylic)
    based on the definition.
    """
    # 1. Load all tools (script-based + extra/MCP tools)
    tool_definitions = definition.get("tool_definitions", [])
    available_tools = load_tools_from_definition(tool_definitions)
    if extra_tools:
        available_tools.update(extra_tools)

    # 2. Determine topology
    is_acrylic = False
    if definition.get("topology") == "custom" or definition.get("topology") == "acrylic":
        is_acrylic = True
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
    return builder.build(definition, available_tools, checkpointer)
