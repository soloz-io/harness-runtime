"""
Graph Builder Module for Agent Executor (DEPRECATED).

**DEPRECATION NOTICE:**
This module is DEPRECATED in favor of the new modular architecture.

**Use this instead:**
    from deepagents_runtime.core import build_agent_from_definition
    agent = build_agent_from_definition(definition)

**Old approach (deprecated):**
    from deepagents_runtime.core import GraphBuilder
    builder = GraphBuilder()
    agent = builder.build_from_definition(definition)

This module is kept for backward compatibility but may be removed in future versions.
The new modular architecture provides:
- Better separation of concerns
- Easier testing and maintenance
- Follows spec-engine pattern

New Modular Structure:
    - core/factory.py: Main entry point (build_agent_from_definition)
    - core/tools/: Tool loading logic
    - core/models/: Model identifier creation
    - core/subagents/: Subagent compilation logic
    - core/custom_graph_builder.py: Custom DAG topology (ADR-005 Path B)
    - core/structured_output.py: Structured I/O (ToolStrategy, DeepSeek thinking disable)

Migration Guide:
    Replace GraphBuilder instances with direct factory calls:

    Before:
        builder = GraphBuilder()
        graph = builder.build_from_definition(definition)

    After:
        graph = build_agent_from_definition(definition)

Classes:
    - GraphBuilder: DEPRECATED - Main class for building LangGraph graphs

Security Notes:
    - This module uses exec() to dynamically load tool code. All agent definitions
      MUST come from trusted sources as they involve executing arbitrary Python code.
    - Tools are executed in an isolated namespace, but this does NOT provide
      complete sandboxing. Production deployments should validate all definitions.

References:
    - Requirements: Req. 3.1 (Stateful Graph Execution)
    - Design: Section 2.11 (Internal Component Architecture)
    - Tasks: Task 6 (Graph Builder Core Logic)
"""

from typing import Any, Dict, List

import structlog
from langchain_core.runnables import Runnable

from core.custom_graph_builder import build_custom_state_graph, is_custom_topology
from core.structured_output import build_tool_strategy, resolve_structured_output_model
from core.subagent_builder import build_subagent
from core.tool_loader import load_tools_from_definition

# Import deep agents pattern components
# Note: The spec requires deepagents package with create_deep_agent and CompiledSubAgent
try:
    from deepagents import create_deep_agent
except ImportError as e:
    raise ImportError(
        "deepagents package is required but not installed. "
        "Install it with: pip install deepagents>=0.2.0"
    ) from e

logger = structlog.get_logger(__name__)


class GraphBuilderError(Exception):
    """Raised when graph building fails."""
    pass


class GraphBuilder:
    """
    Builds LangGraph graphs dynamically from agent definitions.

    This class is responsible for taking an agent definition (JSON structure)
    and compiling it into a runnable LangGraph graph. The process includes:
    1. Loading tools from script definitions
    2. Creating model identifiers for LLM providers
    3. Compiling sub-agents with their tools and prompts
    4. Assembling the main orchestrator graph or custom DAG

    The GraphBuilder reads LLM API keys from environment variables, which are
    populated by Kubernetes Secrets managed by External Secrets Operator.

    Attributes:
        checkpointer: Optional PostgresSaver instance for checkpoint persistence
    """

    def __init__(self, checkpointer: Any = None) -> None:
        """
        Initialize GraphBuilder with checkpointer dependency.

        Args:
            checkpointer: Optional PostgresSaver instance for checkpoint persistence
        """
        self.checkpointer = checkpointer
        logger.info("graph_builder_initialized", has_checkpointer=checkpointer is not None)


    def build_from_definition(self, definition: Dict[str, Any]) -> Runnable[Any, Any]:
        """
        Build a complete LangGraph graph from an agent definition.

        Two paths:
        - Path A (star topology): Orchestrator + specialists via create_deep_agent
        - Path B (custom DAG): raw_agent_definition with conditional edges

        Args:
            definition: Complete agent definition dictionary

        Returns:
            Compiled Runnable graph ready for execution

        Raises:
            GraphBuilderError: If graph building fails at any step
        """
        try:
            logger.info("building_graph_from_definition")

            # Step 1: Load all tools
            tool_definitions = definition.get("tool_definitions", [])
            available_tools = load_tools_from_definition(tool_definitions)

            # Step 1b: Check for custom topology (ADR-005 Path B escape hatch)
            # When present, compile a native StateGraph instead of the default
            # create_deep_agent star topology.
            if is_custom_topology(definition):
                logger.info("detected_custom_topology_building_state_graph")
                return build_custom_state_graph(
                    definition, available_tools, self.checkpointer,
                )

            # Step 2: Parse nodes from definition
            nodes = definition.get("nodes", [])
            if not nodes:
                raise GraphBuilderError("Agent definition must contain at least one node")

            # Step 3: Identify orchestrator and specialist nodes
            orchestrator_config = None
            specialist_configs = []

            for node in nodes:
                node_type = node.get("type", "specialist").lower()
                if node_type == "orchestrator":
                    orchestrator_config = node
                else:
                    specialist_configs.append(node)

            if not orchestrator_config:
                logger.warning("no_orchestrator_found_using_first_node")
                orchestrator_config = nodes[0] if nodes else {}

            logger.info(
                "graph_structure_parsed",
                total_nodes=len(nodes),
                has_orchestrator=bool(orchestrator_config),
                specialist_count=len(specialist_configs)
            )

            # Step 4: Build all sub-agents as CompiledSubAgent instances
            compiled_subagents: List[Any] = []

            for specialist_node in specialist_configs:
                specialist_config = specialist_node.get("config", {})
                sub_agent = build_subagent(specialist_config, available_tools)
                compiled_subagents.append(sub_agent)

            logger.info(
                "compiled_subagents",
                count=len(compiled_subagents),
                names=[
                    sa.get("name") if isinstance(sa, dict) else getattr(sa, "name", "unknown")
                    for sa in compiled_subagents
                ],
            )

            # Step 5: Build the main orchestrator agent
            logger.info("building_orchestrator_agent")

            orchestrator_actual_config = orchestrator_config.get("config", {})

            orchestrator_model_config = orchestrator_actual_config.get("model", {})
            orchestrator_provider = orchestrator_model_config.get("provider", "openai")
            orchestrator_model_name = (
                orchestrator_model_config.get("model_name")
                or orchestrator_model_config.get("model")
            )
            if not orchestrator_model_name:
                raise GraphBuilderError(
                    "Agent definition must specify a model "
                    "(add config.model.model_name to the orchestrator node)"
                )

            orchestrator_system_prompt = orchestrator_actual_config.get("system_prompt", "")

            # Extract optional structured output / schema features
            orchestrator_response_format_raw = orchestrator_actual_config.get("response_format")
            orchestrator_response_format = build_tool_strategy(orchestrator_response_format_raw)
            orchestrator_state_schema = orchestrator_actual_config.get("state_schema")
            orchestrator_context_schema = orchestrator_actual_config.get("context_schema")

            # Extract and resolve orchestrator tools
            orchestrator_tool_names = orchestrator_actual_config.get("tools", [])
            orchestrator_tools = []

            for tool_name in orchestrator_tool_names:
                if tool_name in available_tools:
                    orchestrator_tools.append(available_tools[tool_name])
                else:
                    logger.warning(
                        "orchestrator_tool_not_found",
                        tool_name=tool_name,
                        available_tools=list(available_tools.keys())
                    )

            logger.info(
                "orchestrator_config_extracted",
                orchestrator_name=orchestrator_actual_config.get("name", "unknown"),
                model=str(orchestrator_model_name),
                system_prompt_length=len(orchestrator_system_prompt),
                requested_tools=orchestrator_tool_names,
                resolved_tools=len(orchestrator_tools),
                has_response_format=orchestrator_response_format is not None,
                has_state_schema=orchestrator_state_schema is not None,
                has_context_schema=orchestrator_context_schema is not None,
            )

            # Step 6: Assemble the main graph using create_deep_agent
            logger.info(
                "creating_deep_agent",
                orchestrator_model=str(orchestrator_model_name),
                subagent_count=len(compiled_subagents),
                has_response_format=orchestrator_response_format is not None,
            )

            # Build create_deep_agent kwargs
            deep_agent_kwargs: dict[str, Any] = {
                "model": resolve_structured_output_model(
                    provider=orchestrator_provider,
                    model_name=orchestrator_model_name,
                    response_format=orchestrator_response_format_raw,
                ),
                "system_prompt": orchestrator_system_prompt,
                "tools": orchestrator_tools,
                "subagents": compiled_subagents,
                "checkpointer": self.checkpointer,
            }
            if orchestrator_response_format is not None:
                deep_agent_kwargs["response_format"] = orchestrator_response_format
            if orchestrator_state_schema is not None:
                deep_agent_kwargs["state_schema"] = orchestrator_state_schema
            if orchestrator_context_schema is not None:
                deep_agent_kwargs["context_schema"] = orchestrator_context_schema

            main_runnable = create_deep_agent(**deep_agent_kwargs)

            logger.info(
                "create_deep_agent_result",
                runnable_type=type(main_runnable).__name__,
                has_nodes=hasattr(main_runnable, 'nodes'),
            )

            logger.info(
                "graph_built_successfully",
                orchestrator_name=orchestrator_actual_config.get("name", "main"),
                sub_agent_count=len(compiled_subagents),
                total_tools=len(available_tools),
                graph_type="deep_agent",
            )

            return main_runnable

        except Exception as e:
            logger.error(
                "graph_building_failed",
                error=str(e),
                error_type=type(e).__name__
            )
            raise GraphBuilderError(f"Graph building failed: {e}") from e
