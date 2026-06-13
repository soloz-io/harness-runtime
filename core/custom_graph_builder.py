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
  are incremented via dedicated per-target pass-through nodes
  (__increment_budget_{target}__) so multiple conditional edges do not conflict.
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
from langgraph.graph import END, StateGraph

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
    return any("condition" in edge or "conditions" in edge for edge in edges)


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


def build_node_middleware(
    node_config: Dict[str, Any],
    response_format: Any = None,
) -> list[Any]:
    """Reconstruct the essential deepagents middleware stack for a single node.

    When the node has a response_format, appends StructuredOutputMappingMiddleware
    so structured output fields (e.g. approved, feedback) are spread into typed
    state fields accessible to edge routers.
    """
    middleware: list[Any] = [
        TodoListMiddleware(),
        FilesystemMiddleware(),
        PatchToolCallsMiddleware(),
    ]
    if response_format:
        from core.structured_output import StructuredOutputMappingMiddleware  # noqa: PLC0415
        middleware.append(StructuredOutputMappingMiddleware())
    return middleware


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
    from core.structured_output import needs_thinking_disabled, resolve_structured_output_model  # noqa: PLC0415

    response_format = config.get("response_format")
    model_identifier = ModelFactory.resolve_model_identifier(provider, model_name)

    if needs_thinking_disabled(model_identifier, response_format):
        model = resolve_structured_output_model(provider, model_name, response_format)
    elif "deepseek" in model_identifier.lower():
        # Disable DeepSeek thinking mode for ALL nodes, not just those with
        # response_format, to avoid reasoning_content conflicts when
        # conversation history crosses agent boundaries.
        model = ModelFactory.create_model(
            provider=provider,
            model_name=model_name,
            extra_body={"thinking": {"type": "disabled"}},
        )
    else:
        model = ModelFactory.create_model(provider=provider, model_name=model_name)

    tool_names = config.get("tools", [])
    tools = []
    for name in tool_names:
        if name in available_tools:
            tools.append(available_tools[name])
        else:
            logger.warning("node_tool_not_found", node=node.get("id", "unknown"), tool_name=name)

    response_format = config.get("response_format")
    middleware = build_node_middleware(config, response_format)

    kwargs: dict[str, Any] = {
        "model": model,
        "system_prompt": config.get("system_prompt", ""),
        "tools": tools,
        "middleware": middleware,
        "checkpointer": checkpointer,
        "state_schema": state_schema,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    return create_agent(**kwargs)


# ------------------------------------------------------------------ #
#  Edge routing                                                       #
# ------------------------------------------------------------------ #


def create_edge_router(edge_def: Dict[str, Any]) -> Any:
    """Return a pure LangGraph router function for a conditional edge.

    When the condition evaluates to True and mentions retry_count, routes
    through a per-target budget increment node. Otherwise routes to target
    directly. On False or error, routes to alt_target.
    """
    target: str = edge_def["target"]
    alt_target: str = edge_def.get("alt_target", "__end__")
    if alt_target == "__end__":
        alt_target = END
    condition: str = edge_def.get("condition", "")

    def router(state: dict[str, Any]) -> str:
        if not condition:
            return target
        try:
            result: Any = eval(condition, {"__builtins__": {}}, {"state": state})
            if result:
                if "retry_count" in condition:
                    return f"__increment_budget_{target}__"
                return target
        except Exception as e:
            logger.error("edge_condition_evaluation_failed", condition=condition, error=str(e))
        return alt_target

    return router


def create_combined_edge_router(
    conditions: list[tuple[str, str, bool]],
    default_target: str,
) -> Any:
    """Return a LangGraph router that evaluates conditions in order.

    Each tuple is (condition_expression, target_node, should_increment).
    The first condition that evaluates to True wins:
    - If should_increment is True, routes through __increment_budget_{target}__
    - Otherwise routes to target directly

    If no condition matches, routes to default_target.

    Supports both:
    - New multi-condition format: conditions array from definition JSON
    - Legacy single-condition format: unified into a list of one
    """
    def router(state: dict[str, Any]) -> str:
        for condition_str, target, should_increment in conditions:
            try:
                result: Any = eval(
                    condition_str,
                    {"__builtins__": {}},
                    {"state": state},
                )
                if result:
                    if should_increment:
                        return f"__increment_budget_{target}__"
                    return target
            except Exception as e:
                logger.error(
                    "edge_condition_evaluation_failed",
                    condition=condition_str,
                    error=str(e),
                )
                continue
        return default_target

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

    graph = StateGraph(state_schema)  # type: ignore  # dynamic subclass of DeepAgentState satisfies StateLike

    # 1. Collect per-target increment requirements and add budget nodes
    #    (created before definition nodes to guarantee they exist at compile time)
    added_budget_nodes: set[str] = set()
    increment_targets: set[str] = set()

    for edge in edges:
        if "conditions" in edge:
            for c in edge["conditions"]:
                needs_inc = c.get("increment", False) or "retry_count" in c.get("condition", "")
                if needs_inc:
                    increment_targets.add(c["target"])
        elif "condition" in edge:
            if "retry_count" in edge.get("condition", ""):
                increment_targets.add(edge.get("target") or edge.get("to"))

    for target in increment_targets:
        budget_node_name = f"__increment_budget_{target}__"
        if budget_node_name in added_budget_nodes:
            continue

        def _make_increment_budget() -> Any:
            def increment_budget_node(state: dict[str, Any]) -> dict[str, Any]:
                return {"retry_count": state.get("retry_count", 0) + 1}
            return increment_budget_node

        graph.add_node(budget_node_name, _make_increment_budget())
        graph.add_edge(budget_node_name, target)
        added_budget_nodes.add(budget_node_name)
        logger.info("custom_graph_budget_node_added", name=budget_node_name, target=target)

    # 2. Add all definition nodes
    for node in nodes:
        node_id = node.get("id") or node.get("name")
        if not node_id:
            raise ValueError("Each node must have an 'id' or 'name' field")
        runnable = compile_node(node, available_tools, state_schema, checkpointer)
        graph.add_node(node_id, runnable)
        logger.info("custom_graph_node_added", node_id=node_id, type=node.get("type"))

    # 3. Set entry point
    first_id = nodes[0].get("id") or nodes[0].get("name")
    graph.set_entry_point(first_id)

    # 4. Group conditional edges by source and add edges
    #    (LangGraph allows only one add_conditional_edges per source, so all
    #     conditions from the same source are combined into a single router)

    conditional_groups: dict[str, tuple[list[tuple[str, str, bool]], str]] = {}
    unconditional_edges: list[tuple[str, str]] = []

    for edge in edges:
        source = edge.get("source") or edge.get("from")

        if "conditions" in edge:
            entries: list[tuple[str, str, bool]] = []
            for c in edge["conditions"]:
                cond_str = c["condition"]
                target = c["target"]
                inc = c.get("increment", False) or "retry_count" in cond_str
                entries.append((cond_str, target, inc))
            default_target = edge.get("default_target", END)
            if default_target == "__end__":
                default_target = END

            existing = conditional_groups.get(source)
            if existing is not None:
                existing[0].extend(entries)
            else:
                conditional_groups[source] = (entries, default_target)

        elif "condition" in edge:
            target = edge.get("target") or edge.get("to")
            alt_target = edge.get("alt_target", END)
            if alt_target == "__end__":
                alt_target = END
            condition = edge["condition"]
            inc = "retry_count" in condition
            entries = [(condition, target, inc)]

            existing = conditional_groups.get(source)
            if existing is not None:
                existing[0].extend(entries)
            else:
                conditional_groups[source] = (entries, alt_target)

        else:
            target = edge.get("target") or edge.get("to")
            unconditional_edges.append((source, target))

    for source, (entries, default_target) in conditional_groups.items():
        router = create_combined_edge_router(entries, default_target)
        graph.add_conditional_edges(source, router)
        logger.info(
            "custom_graph_conditional_edge_grouped",
            source=source,
            condition_count=len(entries),
        )

    for source, target in unconditional_edges:
        graph.add_edge(source, target)
        logger.info("custom_graph_edge", source=source, target=target)

    logger.info(
        "custom_graph_assembled",
        node_count=len(nodes) + len(added_budget_nodes),
        edge_count=len(edges),
    )

    return graph.compile(checkpointer=checkpointer)
