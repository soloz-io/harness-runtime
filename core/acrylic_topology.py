"""
Acrylic Topology Builder (Custom DAG) StateGraph Compiler.

Compiles a native LangGraph StateGraph from definition JSON edges,
bypassing orchestrator star topology for code-enforced DAGs.
"""

from typing import Any, Dict, List

import structlog
from langchain_core.runnables import Runnable
from langgraph.graph import END, StateGraph

from core.interfaces import TopologyBuilder
from core.node_compiler import compile_node

try:
    from deepagents.graph import DeepAgentState
except ImportError as e:
    raise ImportError(
        "deepagents package is required but not installed. "
        "Install it with: pip install deepagents>=0.2.0"
    ) from e

logger = structlog.get_logger(__name__)


class AcrylicTopologyBuilder(TopologyBuilder):
    """Builds an acyclic (acrylic) custom DAG topology."""

    def build(
        self,
        definition: Dict[str, Any],
        available_tools: Dict[str, Any],
        checkpointer: Any,
    ) -> Runnable[Any, Any]:
        """Compile a custom StateGraph from raw_agent_definition nodes & edges."""
        nodes = definition.get("nodes", [])
        edges = definition.get("edges", [])
        state_schema = self._build_state_schema(definition)

        graph = StateGraph(state_schema)  # type: ignore

        # 1. Capture initial messages
        def _capture_initial(state: dict[str, Any]) -> dict[str, Any]:
            return {"__initial_messages": list(state.get("messages", []))}

        init_id = "__init_messages__"
        graph.add_node(init_id, _capture_initial)
        graph.set_entry_point(init_id)

        # 2. Collect per-target increment requirements and add budget nodes
        added_budget_nodes = self._add_budget_nodes(graph, edges)

        # 3. Add all definition nodes with isolation
        for node in nodes:
            node_id = node.get("id") or node.get("name")
            if not node_id:
                raise ValueError("Each node must have an 'id' or 'name' field")
            runnable = compile_node(node, available_tools, state_schema, checkpointer)
            self._add_isolated_agent(graph, node_id, runnable, node)

        # 4. Set edge from init to first prep node
        first_id = nodes[0].get("id") or nodes[0].get("name")
        graph.add_edge(init_id, f"__prep_{first_id}__")

        # 5. Group conditional edges by source and add edges
        self._wire_conditional_edges(graph, edges)

        logger.info(
            "custom_graph_assembled",
            node_count=len(nodes) + len(added_budget_nodes),
            edge_count=len(edges),
        )

        return graph.compile(checkpointer=checkpointer)

    def _build_state_schema(self, definition: Dict[str, Any]) -> type:
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

    def _prep_target(self, target: str) -> str:
        if not target or target == "__end__":
            return END
        return f"__prep_{target}__"

    def _add_isolated_agent(
        self,
        graph: StateGraph,
        node_id: str,
        runnable: Runnable[Any, Any],
        node: Dict[str, Any],
    ) -> None:
        prep_node_id = f"__prep_{node_id}__"

        def prep_agent(state: dict[str, Any]) -> dict[str, Any]:
            init_msgs = state.get("__initial_messages", [])
            return {"messages": list(init_msgs)}

        graph.add_node(prep_node_id, prep_agent)
        graph.add_node(node_id, runnable)
        graph.add_edge(prep_node_id, node_id)
        logger.info(
            "custom_graph_node_added",
            node_id=node_id,
            type=node.get("type"),
            isolated=True,
            subgraph=True,
        )

    def _add_budget_nodes(self, graph: StateGraph, edges: List[Dict[str, Any]]) -> set[str]:
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
            graph.add_edge(budget_node_name, self._prep_target(target))
            added_budget_nodes.add(budget_node_name)
            logger.info("custom_graph_budget_node_added", name=budget_node_name, target=target)

        return added_budget_nodes

    def _wire_conditional_edges(self, graph: StateGraph, edges: List[Dict[str, Any]]) -> None:
        conditional_groups: dict[str, tuple[list[tuple[str, str, str, bool]], str]] = {}
        unconditional_edges: list[tuple[str, str]] = []

        for edge in edges:
            source = edge.get("source") or edge.get("from")

            if "conditions" in edge:
                entries: list[tuple[str, str, str, bool]] = []
                for c in edge["conditions"]:
                    cond_str = c["condition"]
                    target = c["target"]
                    inc = c.get("increment", False) or "retry_count" in cond_str
                    entries.append(
                        (cond_str, self._prep_target(target), f"__increment_budget_{target}__", inc)
                    )
                default_target = self._prep_target(edge.get("default_target", "__end__"))

                existing = conditional_groups.get(source)
                if existing is not None:
                    existing[0].extend(entries)
                else:
                    conditional_groups[source] = (entries, default_target)

            elif "condition" in edge:
                target = edge.get("target") or edge.get("to")
                alt_target = self._prep_target(edge.get("alt_target", "__end__"))
                condition = edge["condition"]
                inc = "retry_count" in condition
                entries = [
                    (condition, self._prep_target(target), f"__increment_budget_{target}__", inc)
                ]

                existing = conditional_groups.get(source)
                if existing is not None:
                    existing[0].extend(entries)
                else:
                    conditional_groups[source] = (entries, alt_target)

            else:
                target = edge.get("target") or edge.get("to")
                unconditional_edges.append((source, self._prep_target(target)))

        for source, (entries, default_target) in conditional_groups.items():
            router = self._create_combined_edge_router(entries, default_target)
            graph.add_conditional_edges(source, router)
            logger.info(
                "custom_graph_conditional_edge_grouped",
                source=source,
                condition_count=len(entries),
            )

        for source, target in unconditional_edges:
            graph.add_edge(source, target)
            logger.info("custom_graph_edge", source=source, target=target)

    def _create_combined_edge_router(
        self, conditions: list[tuple[str, str, str, bool]], default_target: str
    ) -> Any:
        def router(state: dict[str, Any]) -> str:
            for condition_str, target, inc_target, should_increment in conditions:
                try:
                    result: Any = eval(
                        condition_str,
                        {"__builtins__": {}},
                        {"state": state},
                    )
                    if result:
                        if should_increment:
                            return inc_target
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
