"""
Hybrid Topology Builder.

One LangGraph orchestrator (``create_deep_agent``) whose specialists are a
mix of three forms, all exposed through the orchestrator's ``task`` tool:

- **Declarative ``SubAgent``** (single ``create_agent``) — the existing star
  specialist form, built by ``build_subagent``.
- **Nested deep agent** (``mode: "deepagent"``) — a ``CompiledSubAgent``
  wrapping its own ``create_deep_agent`` graph with subagent specs, giving
  arbitrary nesting depth.
- **Nested subgraph** (``mode: "subgraph"``) — a ``CompiledSubAgent``
  wrapping an acrylic ``StateGraph`` compiled from nested ``nodes``/``edges``
  (each node a ``create_agent``).

Existing star/acrylic builders are imported read-only and never modified.
"""

from typing import Any, Dict, List

import structlog
from langchain_core.runnables import Runnable

from core.interfaces import TopologyBuilder
from core.topology._shared import (
    build_deep_agent_runnable,
    ensure_artifact_backend,
)
from core.topology.subagent_builder import build_subagent

logger = structlog.get_logger(__name__)


class HybridTopologyBuilder(TopologyBuilder):
    """Builds a hybrid topology: an orchestrator over mixed specialist forms."""

    def build(
        self,
        definition: Dict[str, Any],
        available_tools: Dict[str, Any],
        checkpointer: Any,
        *,
        workspace_id: str | None = None,
        session_id: str | None = None,
        db_pool: Any = None,
        backend: Any = None,
        skills: list[str] | None = None,
        composite_backend: Any = None,
        tools_ctx: Any = None,
    ) -> Runnable[Any, Any]:
        """Build the hybrid graph."""
        nodes = definition.get("nodes", [])
        if not nodes:
            raise ValueError("Agent definition must contain at least one node")

        backend = ensure_artifact_backend(
            backend,
            workspace_id=workspace_id,
            session_id=session_id,
            db_pool=db_pool,
        )

        orchestrator_config = None
        specialist_configs: list[tuple[Dict[str, Any], str]] = []

        for node in nodes:
            node_id = node.get("id", "")
            node_type = node.get("type", "specialist").lower()
            if node_type == "orchestrator":
                orchestrator_config = node
            else:
                specialist_configs.append((node, node_id))

        if not orchestrator_config:
            logger.warning("no_orchestrator_found_using_first_node")
            orchestrator_config = nodes[0] if nodes else {}

        logger.info(
            "hybrid_structure_parsed",
            total_nodes=len(nodes),
            has_orchestrator=bool(orchestrator_config),
            specialist_count=len(specialist_configs),
        )

        subagents = self._compile_specialists(
            specialist_configs,
            available_tools,
            checkpointer=checkpointer,
            backend=backend,
            composite_backend=composite_backend,
            tools_ctx=tools_ctx,
        )

        logger.info(
            "compiled_subagents",
            count=len(subagents),
            names=[sa.get("name", "unknown") for sa in subagents],
        )

        orchestrator_node = orchestrator_config
        orchestrator_node_id = orchestrator_node.get("id", "")
        orchestrator_actual_config = orchestrator_node.get("config", {})

        main_runnable = build_deep_agent_runnable(
            orchestrator_actual_config,
            available_tools,
            checkpointer=checkpointer,
            node_id=orchestrator_node_id,
            subagents=subagents,
            skills=skills,
            composite_backend=composite_backend,
            backend=backend,
            tools_ctx=tools_ctx,
        )

        logger.info(
            "graph_built_successfully",
            orchestrator_name=orchestrator_actual_config.get("name", "main"),
            sub_agent_count=len(subagents),
            graph_type="hybrid_deep_agent",
        )

        return main_runnable

    def _compile_specialists(
        self,
        specialists: List[tuple[Dict[str, Any], str]],
        available_tools: Dict[str, Any],
        *,
        checkpointer: Any,
        backend: Any,
        composite_backend: Any,
        tools_ctx: Any,
    ) -> List[Any]:
        """Compile each specialist into a declarative SubAgent or a
        CompiledSubAgent (nested deep agent / nested subgraph)."""
        compiled: List[Any] = []

        for node, node_id in specialists:
            specialist_config = node.get("config", {})
            mode = specialist_config.get("mode")

            if mode == "deepagent":
                spec = self._build_nested_deep_agent_spec(
                    specialist_config,
                    available_tools,
                    checkpointer=checkpointer,
                    node_id=node_id,
                    backend=backend,
                    composite_backend=composite_backend,
                    tools_ctx=tools_ctx,
                )
                compiled.append(spec)
            elif mode == "subgraph":
                spec = self._build_nested_subgraph_spec(
                    specialist_config,
                    available_tools,
                    checkpointer=checkpointer,
                )
                compiled.append(spec)
            else:
                tools_spec = tools_ctx.node_tools.get(node_id) if tools_ctx else None
                spec = build_subagent(
                    specialist_config,
                    available_tools,
                    skills=specialist_config.get("skills"),
                    tools_spec=tools_spec,
                )
                compiled.append(spec)

            logger.info("specialist_compiled", node_id=node_id, mode=mode or "subagent")

        return compiled

    def _build_nested_deep_agent_spec(
        self,
        specialist_config: Dict[str, Any],
        available_tools: Dict[str, Any],
        *,
        checkpointer: Any,
        node_id: str,
        backend: Any,
        composite_backend: Any,
        tools_ctx: Any,
    ) -> Dict[str, Any]:
        """Compile a ``mode: "deepagent"`` specialist into a CompiledSubAgent.

        Nested subagent specs are declarative ``SubAgent`` dicts built by
        ``build_subagent`` (recursively supporting deeper nesting), then the
        whole node becomes its own ``create_deep_agent`` graph.
        """
        agent_name = specialist_config.get("name", node_id)

        nested_subagent_specs: List[Any] = []
        for spec in specialist_config.get("subagents", []):
            nested_subagent_specs.append(
                build_subagent(
                    spec,
                    available_tools,
                    skills=spec.get("skills"),
                )
            )

        nested_runnable = build_deep_agent_runnable(
            specialist_config,
            available_tools,
            checkpointer=checkpointer,
            node_id=node_id,
            subagents=nested_subagent_specs,
            composite_backend=composite_backend,
            backend=backend,
            tools_ctx=tools_ctx,
        )

        return {
            "name": agent_name,
            "description": specialist_config.get(
                "description",
                f"Nested deep agent '{agent_name}'",
            ),
            "runnable": nested_runnable,
        }

    def _build_nested_subgraph_spec(
        self,
        specialist_config: Dict[str, Any],
        available_tools: Dict[str, Any],
        *,
        checkpointer: Any,
    ) -> Dict[str, Any]:
        """Compile a ``mode: "subgraph"`` specialist into a CompiledSubAgent.

        The nested ``nodes``/``edges``/``state_schema`` are compiled as an
        acrylic-style ``StateGraph`` (each node a ``create_agent``) via the
        existing ``AcrylicTopologyBuilder``.
        """
        from core.topology.acrylic_topology import AcrylicTopologyBuilder  # noqa: PLC0415

        agent_name = specialist_config.get("name", "unnamed_subgraph")

        nested_definition: Dict[str, Any] = {
            "nodes": specialist_config.get("nodes", []),
            "edges": specialist_config.get("edges", []),
        }
        if specialist_config.get("state_schema"):
            nested_definition["state_schema"] = specialist_config["state_schema"]

        nested_runnable = AcrylicTopologyBuilder().build(
            nested_definition,
            available_tools,
            checkpointer,
        )

        return {
            "name": agent_name,
            "description": specialist_config.get(
                "description",
                f"Nested subgraph '{agent_name}'",
            ),
            "runnable": nested_runnable,
        }
