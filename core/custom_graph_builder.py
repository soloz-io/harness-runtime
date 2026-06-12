"""
Custom Graph Builder — ADR-005 Path B (raw_agent_definition) StateGraph Compiler.

WHY THIS FILE EXISTS:
The deepagents library's create_deep_agent() enforces a prompt-based star topology
via SubAgentMiddleware + the task() tool. Routing relies on the LLM deciding to
call task(). This is unsuitable for workflows requiring code-enforced DAGs with
strict execution order, conditional revision loops, and budget counters.

This module compiles a native LangGraph StateGraph from definition JSON edges,
bypassing create_deep_agent() entirely while still using deepagents' lower-level
create_agent() for individual nodes (preserving essential middleware).

Key architectural decisions:
- LangGraph routers are pure functions and cannot mutate state. Budget counters
  are incremented via a dedicated __increment_budget__ pass-through node.
- Each node gets a reconstructed middleware stack [TodoListMiddleware,
  FilesystemMiddleware, PatchToolCallsMiddleware] matching the essential subset
  of create_deep_agent()'s assembly.
- State schema inherits from DeepAgentState (DeltaChannel on messages) to keep
  checkpoint growth at O(N) instead of O(N^2) for long-running revision loops.
- Edge conditions use restricted eval() — safe because raw_agent_definition is
  a trusted escape hatch (see ADR-005).
"""

from typing import Any, Dict, List

import structlog
from langchain_core.runnables import Runnable
from langgraph.graph import StateGraph

# Import deepagents components for individual agent node construction
try:
    from deepagents.graph import DeepAgentState
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from langchain.agents import create_agent
    from langchain.agents.middleware import TodoListMiddleware
except ImportError as e:
    raise ImportError(
        "deepagents package is required but not installed. "
        "Install it with: pip install deepagents>=0.2.0"
    ) from e

logger = structlog.get_logger(__name__)


# ------------------------------------------------------------------ #
#  Detection                                                        #
# ------------------------------------------------------------------ #


def is_custom_topology(definition: Dict[str, Any]) -> bool:
    """Return True if edges indicate a non-star topology requiring a StateGraph."""
    if definition.get("topology") == "custom":
        return True
    edges = definition.get("edges", [])
    return any("condition" in edge for edge in edges)


# ------------------------------------------------------------------ #
#  State schema                                                      #
# ------------------------------------------------------------------ #


def build_state_schema(definition: Dict[str, Any]) -> type:
    """Create a DeepAgentState subclass with additional fields from definition['state_schema']."""
    schema_config = definition.get("state_schema", {})
    if not schema_config:
        return DeepAgentState

    annotations: dict[str, Any] = {}
    for field_name, field_config in schema_config.items():
        field_type = field_config.get("type", "any")
        if field_type == "int":
            annotations[field_name] = int
        elif field_type == "str":
            annotations[field_name] = str
        elif field_type == "bool":
            annotations[field_name] = bool
        elif field_type == "float":
            annotations[field_name] = float
        elif field_type == "dict":
            annotations[field_name] = Dict[str, Any]
        elif field_type == "list":
            annotations[field_name] = List[Any]
        else:
            annotations[field_name] = Any

    DynamicState = type(
        "CustomTopologyState",
        (DeepAgentState,),
        {"__annotations__": annotations},
    )
    logger.info("custom_state_schema_created", fields=list(annotations.keys()))
    return DynamicState


# ------------------------------------------------------------------ #
#  Middleware & node compilation                                      #
# ------------------------------------------------------------------ #


def build_node_middleware(node_config: Dict[str, Any]) -> list[Any]:
    """Reconstruct the essential deepagents middleware stack for a single node."""
    return [
        TodoListMiddleware(),
        FilesystemMiddleware(),
        PatchToolCallsMiddleware(),
    ]


def compile_node(
    node: Dict[str, Any],
    available_tools: Dict[str, Any],
    state_schema: type,
    checkpointer: Any,
) -> Runnable[Any, Any]:
    """Compile a single node from its JSON config into a create_agent() runnable."""
    config = node.get("config", {})
    model_cfg = config.get("model", {})
    provider = model_cfg.get("provider", "openai")
    model_name = model_cfg.get("model_name") or model_cfg.get("model")

    from core.model_factory import ModelFactory  # noqa: PLC0415
    model = ModelFactory.create_model(provider=provider, model_name=model_name)

    tool_names = config.get("tools", [])
    tools = []
    for name in tool_names:
        if name in available_tools:
            tools.append(available_tools[name])
        else:
            logger.warning("node_tool_not_found", node=node.get("id", "unknown"), tool_name=name)

    middleware = build_node_middleware(config)

    return create_agent(
        model,
        system_prompt=config.get("system_prompt", ""),
        tools=tools,
        middleware=middleware,
        checkpointer=checkpointer,
        state_schema=state_schema,
    )


# ------------------------------------------------------------------ #
#  Edge routing                                                       #
# ------------------------------------------------------------------ #


def create_edge_router(edge_def: Dict[str, Any]) -> Any:
    """Return a pure LangGraph router function for a conditional edge."""
    condition: str = edge_def.get("condition", "")
    alt_target: str = edge_def.get("alt_target", "__end__")

    def router(state: dict[str, Any]) -> str:
        try:
            result: Any = eval(condition, {"__builtins__": {}}, {"state": state})
            if result:
                return "__increment_budget__"
        except Exception as e:
            logger.error("edge_condition_evaluation_failed", condition=condition, error=str(e))
        return alt_target

    return router


# ------------------------------------------------------------------ #
#  Graph assembly                                                     #
# ------------------------------------------------------------------ #


def build_custom_state_graph(
    definition: Dict[str, Any],
    available_tools: Dict[str, Any],
    checkpointer: Any,
) -> Runnable[Any, Any]:
    """Compile a custom StateGraph from raw_agent_definition nodes & edges."""
    nodes = definition.get("nodes", [])
    edges = definition.get("edges", [])
    state_schema = build_state_schema(definition)

    graph: StateGraph[Any] = StateGraph(state_schema)

    # 1. Add all definition nodes
    for node in nodes:
        node_id = node.get("id") or node.get("name")
        if not node_id:
            raise ValueError("Each node must have an 'id' or 'name' field")
        runnable = compile_node(node, available_tools, state_schema, checkpointer)
        graph.add_node(node_id, runnable)
        logger.info("custom_graph_node_added", node_id=node_id, type=node.get("type"))

    # 2. Add the invisible budget increment node (pure state mutation)
    def increment_budget_node(state: dict[str, Any]) -> dict[str, Any]:
        current = state.get("retry_count", 0)
        return {"retry_count": current + 1}

    graph.add_node("__increment_budget__", increment_budget_node)  # type: ignore[type-var]
    logger.info("custom_graph_budget_node_added")

    # 3. Set entry point
    first_id = nodes[0].get("id") or nodes[0].get("name")
    graph.set_entry_point(first_id)

    # 4. Add edges
    for edge in edges:
        source = edge.get("source") or edge.get("from")
        target = edge.get("target") or edge.get("to")

        if "condition" in edge:
            router = create_edge_router(edge)
            graph.add_conditional_edges(source, router)
            graph.add_edge("__increment_budget__", target)
            logger.info(
                "custom_graph_conditional_edge",
                source=source,
                target=target,
                condition=edge.get("condition"),
            )
        else:
            graph.add_edge(source, target)
            logger.info("custom_graph_edge", source=source, target=target)

    logger.info(
        "custom_graph_assembled",
        node_count=len(nodes) + 1,
        edge_count=len(edges),
    )

    return graph.compile(checkpointer=checkpointer)
