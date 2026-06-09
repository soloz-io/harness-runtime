"""
Tool Loading Module for Agent Executor.

This module provides functionality for dynamically loading tools from script definitions.
Tools are loaded by executing Python code that creates BaseTool instances.

Functions:
    - load_tools_from_definition: Load tools from script definitions

Security Notes:
    - This module uses exec() to dynamically load tool code. All tool definitions
      MUST come from trusted sources as they involve executing arbitrary Python code.
    - Tools are executed in an isolated namespace, but this does NOT provide
      complete sandboxing. Production deployments should validate all definitions.

References:
    - Requirements: Req. 3.1 (Stateful Graph Execution)
    - Design: Section 2.12 (Dynamic Tool Loading)
"""

import structlog
from typing import Any, Dict, List
from langchain_core.tools import BaseTool

logger = structlog.get_logger(__name__)


class ToolLoadingError(Exception):
    """Raised when tool loading fails."""
    pass


def load_tools_from_definition(
    tool_definitions: List[Dict[str, Any]]
) -> Dict[str, BaseTool]:
    """
    Dynamically load tools from script definitions.

    This function takes a list of tool definitions (each containing a 'script'
    field with Python code) and executes them to create BaseTool instances.

    **SECURITY WARNING**: This function uses exec() to execute arbitrary Python
    code. All tool definitions MUST come from trusted sources. This approach
    is suitable for controlled environments where agent definitions are created
    by authorized users only.

    The tool script is executed in an isolated namespace that includes:
    - Standard library imports
    - LangChain tool utilities
    - Common third-party libraries (as needed)

    After execution, the namespace is searched for instances of BaseTool,
    which are then extracted and returned.

    Args:
        tool_definitions: List of tool definition dictionaries, each containing:
            - name: Tool identifier (string)
            - script: Python code that creates a BaseTool instance (string)
            - description: Human-readable tool description (optional)

    Returns:
        Dictionary mapping tool names to BaseTool instances

    Raises:
        ToolLoadingError: If tool script execution fails or no BaseTool found

    Example tool definition:
        {
            "name": "web_search",
            "script": '''
            from langchain_community.tools import TavilySearchResults
            web_search_tool = TavilySearchResults(max_results=5)
            ''',
            "description": "Search the web for information"
        }

    References:
        - Requirements: Req. 3.1
        - Design: Section 2.12 (Dynamic Tool Loading)
    """
    if not tool_definitions:
        logger.warning("no_tool_definitions_provided")
        return {}

    loaded_tools: Dict[str, BaseTool] = {}

    for tool_def in tool_definitions:
        tool_name = tool_def.get("name", "unknown")
        
        # Extract script from runtime configuration
        runtime_config = tool_def.get("runtime", {})
        tool_script = runtime_config.get("script", "")

        if not tool_script:
            logger.warning(
                "tool_script_empty",
                tool_name=tool_name,
                has_runtime=bool(tool_def.get("runtime"))
            )
            continue

        try:
            logger.info(
                "loading_tool",
                tool_name=tool_name
            )

            # Create isolated namespace for tool execution
            # Include common imports needed for tool creation
            namespace: Dict[str, Any] = {
                "__builtins__": __builtins__,
                # Add common imports that tools might need
                "BaseTool": BaseTool,
            }

            # SECURITY WARNING: exec() executes arbitrary code
            # Only use with trusted tool definitions
            exec(tool_script, namespace)

            # Extract BaseTool instances from namespace
            tool_instance = None
            for key, value in namespace.items():
                if isinstance(value, BaseTool):
                    tool_instance = value
                    break

            if tool_instance is None:
                raise ToolLoadingError(
                    f"Tool script for '{tool_name}' did not create a BaseTool instance"
                )

            loaded_tools[tool_name] = tool_instance

            logger.info(
                "tool_loaded_successfully",
                tool_name=tool_name,
                tool_type=type(tool_instance).__name__
            )

        except Exception as e:
            logger.error(
                "tool_loading_failed",
                tool_name=tool_name,
                error=str(e),
                error_type=type(e).__name__
            )
            raise ToolLoadingError(
                f"Failed to load tool '{tool_name}': {e}"
            ) from e

    logger.info(
        "tools_loaded",
        total_tools=len(loaded_tools),
        tool_names=list(loaded_tools.keys())
    )

    return loaded_tools
