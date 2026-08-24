"""
Composite Topology Builder.

One LangGraph orchestrator (``create_agent``) with a minimal middleware stack
(HumanInteractionMiddleware + CodeInterpreterMiddleware + SubAgentMiddleware)
whose specialists are all deep agents (``create_deep_agent``), all exposed
through the orchestrator's ``task`` tool:

- **Deep agent specialist** (default) — a ``CompiledSubAgent`` wrapping a
  ``create_deep_agent`` graph. May optionally have nested subagent specs
  via ``config.subagents``.
- **Nested subgraph** (``runtime: "subgraph"``) — a ``CompiledSubAgent``
  wrapping an acrylic ``StateGraph`` compiled from nested ``nodes``/``edges``
  (each node a ``create_agent``).

Existing star/acrylic builders are imported read-only and never modified.
"""

from typing import Any, Dict, List

import structlog

# Add these new imports for the orchestrator state fix
from deepagents import DeepAgentState
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.runnables import Runnable
from langchain_quickjs import CodeInterpreterMiddleware

from core.interfaces import TopologyBuilder
from core.middleware.human_interaction import HumanInteractionMiddleware
from core.middleware.structured_output import (
    build_tool_strategy,
    resolve_structured_output_model,
)
from core.topology._shared import (
    build_deep_agent_runnable,
    ensure_artifact_backend,
)
from core.topology.subagent_builder import build_subagent

logger = structlog.get_logger(__name__)

try:
    from deepagents import SubAgentMiddleware
except ImportError as e:
    raise ImportError(
        "deepagents package is required but not installed. "
        "Install it with: pip install deepagents>=0.2.0"
    ) from e

try:
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware
except ImportError as e:
    raise ImportError(
        "langchain package is required but not installed. "
        "Install it with: pip install langchain>=1.0.0"
    ) from e


class CompositeTopologyBuilder(TopologyBuilder):
    """Builds a composite topology: a pure orchestrator graph over deep agent specialists."""

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
            "composite_structure_parsed",
            total_nodes=len(nodes),
            has_orchestrator=bool(orchestrator_config),
            specialist_count=len(specialist_configs),
        )

        compiled_subagents = self._compile_specialists(
            specialist_configs,
            available_tools,
            backend=backend,
            composite_backend=composite_backend,
            tools_ctx=tools_ctx,
        )

        logger.info(
            "compiled_subagents",
            count=len(compiled_subagents),
            names=[sa.get("name", "unknown") for sa in compiled_subagents],
        )

        orchestrator_node = orchestrator_config
        orchestrator_node_id = orchestrator_node.get("id", "")
        orchestrator_actual_config = orchestrator_node.get("config", {})

        main_runnable = self._build_orchestrator(
            orchestrator_actual_config,
            compiled_subagents,
            checkpointer=checkpointer,
            node_id=orchestrator_node_id,
            backend=backend,
            composite_backend=composite_backend,
            tools_ctx=tools_ctx,
        )

        logger.info(
            "graph_built_successfully",
            orchestrator_name=orchestrator_actual_config.get("name", "main"),
            sub_agent_count=len(compiled_subagents),
            graph_type="composite",
        )

        return main_runnable

    def _build_orchestrator(
        self,
        config: Dict[str, Any],
        compiled_subagents: list[Any],
        *,
        checkpointer: Any,
        node_id: str,
        backend: Any,
        composite_backend: Any,
        tools_ctx: Any,
    ) -> Runnable[Any, Any]:
        """Build the orchestrator as a create_agent graph with essential middleware.

        Middleware stack: HumanInteractionMiddleware (ask_user)
        + CodeInterpreterMiddleware (eval from quickjs) + SubAgentMiddleware (task).
        """
        model_config = config.get("model", {})
        provider = model_config.get("provider", "openai")
        model_name = model_config.get("model_name") or model_config.get("model")
        if not model_name:
            raise ValueError("Orchestrator must specify config.model.model_name")

        system_prompt = config.get("system_prompt", "")
        response_format_raw = config.get("response_format")
        response_format = build_tool_strategy(response_format_raw)

        model = resolve_structured_output_model(
            provider=provider,
            model_name=model_name,
            response_format=response_format_raw,
        )

        # FIX: Provide the orchestrator with FilesystemMiddleware so it owns the files channel
        # Middleware stack: HumanInteraction + CodeInterpreter + SubAgent(task)
        orchestrator_backend = composite_backend or backend
        middleware_stack: list[Any] = [
            TodoListMiddleware(),
            FilesystemMiddleware(backend=orchestrator_backend),
            HumanInteractionMiddleware(),
            CodeInterpreterMiddleware(timeout=300),
        ]

        # FIX: Provide DeepAgentState to SubAgentMiddleware
        if compiled_subagents:
            subagent_middleware = SubAgentMiddleware(
                backend=orchestrator_backend,
                subagents=compiled_subagents,
                state_schema=DeepAgentState,
            )
            middleware_stack.append(subagent_middleware)
            logger.info(
                "subagent_middleware_wired",
                subagent_count=len(compiled_subagents),
            )

        # HumanInTheLoop for ask_user interrupts
        interrupt_on_config = config.get("interrupt_on")
        if interrupt_on_config:
            middleware_stack.append(HumanInTheLoopMiddleware(interrupt_on=interrupt_on_config))

        # Build orchestrator via create_agent
        kwargs: dict[str, Any] = {
            "model": model,
            "system_prompt": system_prompt,
            "tools": [],
            "middleware": middleware_stack,
            "checkpointer": checkpointer,
            # FIX: Ensure state schema has `files` channel
            "state_schema": DeepAgentState,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        logger.info(
            "orchestrator_build_start",
            model_name=str(model_name),
            has_interrupt=bool(interrupt_on_config),
            subagent_count=len(compiled_subagents),
        )

        return create_agent(**kwargs)

    def _compile_specialists(
        self,
        specialists: List[tuple[Dict[str, Any], str]],
        available_tools: Dict[str, Any],
        *,
        backend: Any,
        composite_backend: Any,
        tools_ctx: Any,
    ) -> List[Any]:
        """Compile each specialist into a CompiledSubAgent.

        All specialists are deep agents (create_deep_agent). Specialists with
        ``config.subagents`` get nested subagents. Specialists with
        ``runtime: "subgraph"`` get an acrylic subgraph.

        Specialists are compiled without a checkpointer so that only the
        top-level orchestrator persists state to Postgres.
        """
        compiled: List[Any] = []

        for node, node_id in specialists:
            specialist_config = node.get("config", {})
            runtime = specialist_config.get("runtime")

            if runtime == "subgraph":
                spec = self._build_nested_subgraph_spec(
                    specialist_config,
                    available_tools,
                )
                compiled.append(spec)
            else:
                # All non-subgraph specialists are deep agents
                spec = self._build_deep_agent_spec(
                    specialist_config,
                    available_tools,
                    node_id=node_id,
                    backend=backend,
                    composite_backend=composite_backend,
                    tools_ctx=tools_ctx,
                )
                compiled.append(spec)

            logger.info("specialist_compiled", node_id=node_id, runtime=runtime or "deepagent")

        return compiled

    def _build_deep_agent_spec(
        self,
        specialist_config: Dict[str, Any],
        available_tools: Dict[str, Any],
        *,
        node_id: str,
        backend: Any,
        composite_backend: Any,
        tools_ctx: Any,
    ) -> Dict[str, Any]:
        """Build a specialist as a CompiledSubAgent wrapping create_deep_agent.

        Specialists with ``config.subagents`` get nested subagent specs built
        via ``build_subagent``, giving arbitrary nesting depth.

        Specialists get ``checkpointer=None`` so only the orchestrator persists.
        """
        agent_name = specialist_config.get("name", node_id)

        # Build nested subagent specs (if any)
        nested_subagent_specs: List[Any] = []
        for spec in specialist_config.get("subagents", []):
            sub_name = spec.get("name", "")
            sub_id = spec.get("id", "")
            tools_spec = None
            if tools_ctx:
                tools_spec = tools_ctx.node_tools.get(sub_name) or tools_ctx.node_tools.get(sub_id)
            nested_subagent_specs.append(
                build_subagent(
                    spec,
                    available_tools,
                    skills=spec.get("skills"),
                    tools_spec=tools_spec,
                )
            )

        nested_runnable = build_deep_agent_runnable(
            specialist_config,
            available_tools,
            checkpointer=None,  # Specialists DO NOT get checkpointer
            node_id=node_id,
            subagents=nested_subagent_specs or None,
            composite_backend=composite_backend,
            backend=backend,
            tools_ctx=tools_ctx,
        )

        return {
            "name": agent_name,
            "description": specialist_config.get(
                "description",
                f"Deep agent '{agent_name}'",
            ),
            "runnable": nested_runnable,
        }

    def _build_nested_subgraph_spec(
        self,
        specialist_config: Dict[str, Any],
        available_tools: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compile a ``runtime: "subgraph"`` specialist into a CompiledSubAgent.

        The nested ``nodes``/``edges``/``state_schema`` are compiled as an
        acrylic-style ``StateGraph`` (each node a ``create_agent``) via the
        existing ``AcrylicTopologyBuilder``.

        Nested subgraphs get ``checkpointer=None`` so only the orchestrator
        persists state to Postgres.
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
            checkpointer=None,  # Subgraphs DO NOT get checkpointer
        )

        return {
            "name": agent_name,
            "description": specialist_config.get(
                "description",
                f"Nested subgraph '{agent_name}'",
            ),
            "runnable": nested_runnable,
        }
