"""
Factory entry point for graph building.
"""

from typing import Any

from langchain_core.runnables import Runnable

from core.graph.interfaces import TopologyBuilder
from core.graph.topology.acrylic_topology import AcrylicTopologyBuilder
from core.graph.topology.composite_topology import CompositeTopologyBuilder
from core.graph.topology.star_topology import StarTopologyBuilder
from core.tools.loader import load_tools_from_definition
from core.tools.registry import ToolRegistry


def build_agent_from_definition(
    definition: dict[str, Any],
    checkpointer: Any = None,
    tool_registry: ToolRegistry | None = None,
    *,
    workspace_id: str | None = None,
    session_id: str | None = None,
    db_pool: Any = None,
    backend: Any = None,
    skills: list[str] | None = None,
    composite_backend: Any = None,
    tools_ctx: Any = None,
) -> Runnable[Any, Any]:
    """
    Build a complete LangGraph graph from an agent definition.

    Delegates to the topology builder the definition names: star, composite or
    acrylic.
    """
    # 1. Resolve all tools from the ToolRegistry
    tool_definitions = definition.get("tool_definitions", [])
    available_tools = load_tools_from_definition(
        tool_definitions,
        registry=tool_registry,
    )

    # 2. Determine topology
    #
    # Three values, and every one of them is a DAG -- which is why none of them
    # says so. ``star`` is an orchestrator with specialists around it, ``composite``
    # is that with specialists that may nest their own subagents, and
    # ``acrylic`` is a code-enforced graph whose edges carry conditions. An absent
    # value is read from the edges: acrylic when any edge carries one, star
    # otherwise.
    topology = definition.get("topology", "")

    # Composite: one orchestrator over a mix of declarative subagents, nested
    # deep agents, and nested acrylic subgraphs. Kept separate from the
    # star/acrylic selection below.
    if topology == "composite":
        builder = CompositeTopologyBuilder()
        composite_build_kwargs: dict[str, Any] = {
            "workspace_id": workspace_id,
            "session_id": session_id,
            "db_pool": db_pool,
            "backend": backend,
            "skills": skills,
            "composite_backend": composite_backend,
            "tools_ctx": tools_ctx,
        }
        return builder.build(
            definition,
            available_tools,
            checkpointer,
            **composite_build_kwargs,
        )

    is_acrylic = False
    if topology == "acrylic":
        is_acrylic = True
    elif topology == "star":
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
    build_kwargs: dict[str, Any] = {
        "workspace_id": workspace_id,
        "session_id": session_id,
        "db_pool": db_pool,
        "backend": backend,
        "tools_ctx": tools_ctx,
    }
    if not is_acrylic:
        build_kwargs["skills"] = skills
        build_kwargs["composite_backend"] = composite_backend
    return builder.build(
        definition,
        available_tools,
        checkpointer,
        **build_kwargs,
    )
