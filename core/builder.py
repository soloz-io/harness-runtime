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

import structlog
from typing import Any, Dict, List, Optional
from langchain_core.tools import BaseTool
from langchain_core.runnables import Runnable

from core.model_identifier import create_model_identifier
from core.subagent_builder import build_subagent
from core.tool_loader import load_tools_from_definition

# Import deep agents pattern components
# Note: The spec requires deepagents package with create_deep_agent and CompiledSubAgent
try:
    from deepagents import create_deep_agent
    from langchain.agents import create_agent
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
    4. Assembling the main orchestrator graph

    The GraphBuilder reads LLM API keys from environment variables, which are
    populated by Kubernetes Secrets managed by External Secrets Operator.

    Attributes:
        checkpointer: Optional PostgresSaver instance for checkpoint persistence

    Example:
        builder = GraphBuilder()
        graph = builder.build_from_definition(agent_definition)

        # Execute the compiled graph
        result = graph.invoke({"messages": [{"role": "user", "content": "Hello"}]})
    """

    def __init__(self, checkpointer: Any = None) -> None:
        """
        Initialize GraphBuilder with checkpointer dependency.

        Args:
            checkpointer: Optional PostgresSaver instance for checkpoint persistence

        References:
            - Requirements: Req. 3.1, 14.2
            - Design: Section 2.11 (Component Interaction)
            - Tasks: Task 1.1
        """
        self.checkpointer = checkpointer
        logger.info("graph_builder_initialized", has_checkpointer=checkpointer is not None)


    def build_from_definition(self, definition: Dict[str, Any]) -> Runnable:
        """
        Build a complete LangGraph graph from an agent definition.

        This is the main public method of GraphBuilder. It orchestrates the entire
        graph construction process:
        1. Load all tools from tool definitions
        2. Parse the graph structure (nodes, edges)
        3. Identify orchestrator and specialist nodes
        4. Compile all sub-agents
        5. Assemble the main orchestrator graph
        6. Return the compiled runnable

        The resulting graph can be executed with .invoke() or .stream() methods.

        Args:
            definition: Complete agent definition dictionary containing:
                - tool_definitions: List of tool script definitions
                - nodes: List of node definitions (orchestrator + specialists)
                - edges: Graph edge definitions
                - initial_state: Optional initial state configuration

        Returns:
            Compiled Runnable graph ready for execution

        Raises:
            GraphBuilderError: If graph building fails at any step

        Example definition structure:
            {
                "tool_definitions": [
                    {"name": "web_search", "script": "...", "description": "..."}
                ],
                "nodes": [
                    {
                        "type": "orchestrator",
                        "name": "main_orchestrator",
                        "model": {"provider": "openai", "model_name": "gpt-4o"},
                        "system_prompt": "You coordinate the specialists...",
                        "tools": []
                    },
                    {
                        "type": "specialist",
                        "name": "research_specialist",
                        "model": {"provider": "openai", "model_name": "gpt-4o"},
                        "system_prompt": "You research information...",
                        "tools": ["web_search"]
                    }
                ],
                "edges": [
                    {"from": "orchestrator", "to": "research_specialist"},
                    {"from": "research_specialist", "to": "orchestrator"}
                ]
            }

        References:
            - Requirements: Req. 3.1 (Stateful Graph Execution)
            - Design: Section 3.2 (Core Logic Layer)
            - Tasks: Task 6.5 (Main Graph Builder)
        """
        try:
            logger.info("building_graph_from_definition")

            # Step 1: Load all tools
            tool_definitions = definition.get("tool_definitions", [])
            available_tools = load_tools_from_definition(tool_definitions)

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
            compiled_subagents: List[Any] = []  # List[CompiledSubAgent] when deepagents available

            for specialist_node in specialist_configs:
                # Extract config from node structure
                specialist_config = specialist_node.get("config", {})
                specialist_name = specialist_config.get("name", "unknown")
                sub_agent = build_subagent(specialist_config, available_tools)
                compiled_subagents.append(sub_agent)

            logger.info(
                "compiled_subagents",
                count=len(compiled_subagents),
                names=[
                    sa.get("name") if isinstance(sa, dict) else getattr(sa, "name", "unknown")
                    for sa in compiled_subagents
                ],
                types=[
                    "SubAgent_dict" if isinstance(sa, dict) else "CompiledSubAgent"
                    for sa in compiled_subagents
                ]
            )

            # Step 5: Build the main orchestrator agent
            logger.info("building_orchestrator_agent")

            # Extract orchestrator config from node structure
            orchestrator_actual_config = orchestrator_config.get("config", {})
            
            # Extract orchestrator model configuration
            orchestrator_model_config = orchestrator_actual_config.get("model", {})
            orchestrator_provider = orchestrator_model_config.get("provider", "openai")
            # Support both "model_name" and "model" field names
            orchestrator_model_name = orchestrator_model_config.get("model") or orchestrator_model_config.get("model", "gpt-4.1.mini")
            orchestrator_model_identifier = create_model_identifier(
                orchestrator_provider,
                orchestrator_model_name
            )

            orchestrator_system_prompt = orchestrator_actual_config.get("system_prompt", "")
            
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
            
            # Log orchestrator configuration for verification
            logger.info(
                "orchestrator_config_extracted",
                orchestrator_name=orchestrator_actual_config.get("name", "unknown"),
                model_identifier=orchestrator_model_identifier,
                system_prompt_length=len(orchestrator_system_prompt),
                system_prompt_preview=orchestrator_system_prompt[:200] if orchestrator_system_prompt else "EMPTY",
                requested_tools=orchestrator_tool_names,
                resolved_tools=len(orchestrator_tools),
                tool_names=[t.name if hasattr(t, 'name') else str(t) for t in orchestrator_tools],
                has_task_tool_instruction="task()" in orchestrator_system_prompt
            )

            # Step 6: Assemble the main graph using create_deep_agent
            # Use create_deep_agent with the list of CompiledSubAgent instances
            logger.info(
                "creating_deep_agent",
                orchestrator_model=orchestrator_model_identifier,
                subagent_count=len(compiled_subagents),
                subagent_names=[
                    sa.get("name") if isinstance(sa, dict) else getattr(sa, "name", "unknown")
                    for sa in compiled_subagents
                ],
                subagent_types=[
                    type(sa).__name__ if not isinstance(sa, dict) else "dict"
                    for sa in compiled_subagents
                ],
                has_checkpointer=self.checkpointer is not None
            )
            
            # Log detailed subagent info for debugging
            for i, sa in enumerate(compiled_subagents):
                if isinstance(sa, dict):
                    logger.info(
                        f"subagent_{i}_details",
                        name=sa.get("name"),
                        description=sa.get("description", "")[:100],
                        has_system_prompt=bool(sa.get("system_prompt")),
                        has_tools=len(sa.get("tools", [])),
                        model=sa.get("model")
                    )
            
            # Initialize the model object from the identifier string
            # create_deep_agent expects a model object, not a string
            # Use ModelFactory for clean separation of mock vs real models
            from core.model_factory import ModelFactory
            orchestrator_model = ModelFactory.create_model()
            logger.info("model_created_via_factory", model_type=type(orchestrator_model).__name__)
            
            main_runnable = create_deep_agent(
                model=orchestrator_model,
                system_prompt=orchestrator_system_prompt,
                tools=orchestrator_tools,  # Pass resolved orchestrator tools
                subagents=compiled_subagents,  # List of CompiledSubAgent and SubAgent dict instances
                checkpointer=self.checkpointer,  # Pass checkpointer for state persistence
            )
            
            # Debug: Check if the graph has the expected structure
            logger.info(
                "create_deep_agent_result",
                runnable_type=type(main_runnable).__name__,
                has_nodes=hasattr(main_runnable, 'nodes'),
                node_count=len(getattr(main_runnable, 'nodes', {})) if hasattr(main_runnable, 'nodes') else 0
            )

            logger.info(
                "graph_built_successfully",
                orchestrator_name=orchestrator_actual_config.get("name", "main"),
                orchestrator_model=orchestrator_model_identifier,
                sub_agent_count=len(compiled_subagents),
                total_tools=len(available_tools),
                graph_type="deep_agent"
            )

            return main_runnable



        except Exception as e:
            logger.error(
                "graph_building_failed",
                error=str(e),
                error_type=type(e).__name__
            )
            raise GraphBuilderError(f"Graph building failed: {e}") from e
