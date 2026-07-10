"""
Subagent Builder Module for Agent Executor.

This module provides functionality for compiling sub-agents (specialist agents)
from configuration dictionaries. It creates CompiledSubAgent instances that can
be used in deepagents workflows.

Functions:
    - build_subagent: Compile a sub-agent from specialist configuration

References:
    - Requirements: Req. 3.1 (Stateful Graph Execution)
    - Design: Section 3.2.2 (Sub-Agent Compilation)
    - Spec: build_agent_from_definition.md (create_compiled_subagent)
"""

from typing import Any, Dict, List

import structlog
from langchain_core.tools import BaseTool

from core.middleware.rubric_middleware import build_rubric_middlewares
from core.model_factory import ModelFactory

# Import deep agents pattern components
# Note: The spec requires deepagents package with create_deep_agent and CompiledSubAgent
try:
    from deepagents import CompiledSubAgent
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from langchain.agents import create_agent

    from core.middleware.human_interaction import HumanInteractionMiddleware
    from core.state_schema_builder import create_state_schema_from_config

    DEEPAGENTS_AVAILABLE = True
except ImportError:
    # Fallback to LangGraph's create_react_agent if deepagents not available
    DEEPAGENTS_AVAILABLE = False
    import warnings

    warnings.warn(
        "deepagents package not available. Using fallback create_react_agent. "
        "Install deepagents for full deep agent support.",
        ImportWarning,
        stacklevel=2,
    )

from langchain.agents.middleware import HumanInTheLoopMiddleware

try:
    from core.middleware.github_middleware import GitHubMiddleware
    from core.middleware.github_middleware import execute_shell as _git_execute_shell
    from core.middleware.github_middleware import open_pull_request as _git_open_pull_request

    HAS_GITHUB_MIDDLEWARE = True
except ImportError:
    HAS_GITHUB_MIDDLEWARE = False

logger = structlog.get_logger(__name__)


class SubAgentCompilationError(Exception):
    """Raised when sub-agent compilation fails."""

    pass


def build_subagent(
    specialist_config: Dict[str, Any], available_tools: Dict[str, BaseTool]
) -> Any:  # Returns SubAgent dict or CompiledSubAgent
    """
    Compile a sub-agent from specialist configuration.

    This function creates a specialized agent (sub-agent) that will be part of
    the larger orchestrator graph. It returns either a SubAgent dict or
    CompiledSubAgent depending on the configuration.

    Strategy:
    - For simple agents (no tools or few tools): Return SubAgent dict
      (let SubAgentMiddleware build the runnable)
    - For complex agents (many tools or custom middleware): Return CompiledSubAgent
      (pre-build the runnable for more control)

    Args:
        specialist_config: Configuration dictionary containing:
            - name: Sub-agent identifier (string)
            - model: Model configuration (dict with provider, model_name)
            - system_prompt: Instructions for the agent (string)
            - tools: List of tool names this agent can use (list of strings)
            - description: Optional brief description (for SubAgent dict)
        available_tools: Dictionary of all loaded tools (from load_tools_from_definition)

    Returns:
        SubAgent dict or CompiledSubAgent instance

    Raises:
        SubAgentCompilationError: If sub-agent compilation fails

    References:
        - Requirements: Req. 3.1, 14.2
        - Design: Section 3.2.2 (Sub-Agent Compilation)
        - Spec: build_agent_from_definition.md (create_compiled_subagent)
        - Tasks: Task 1.1
    """
    agent_name = specialist_config.get("name", "unnamed_agent")

    try:
        logger.info(
            "building_subagent", agent_name=agent_name, using_deepagents=DEEPAGENTS_AVAILABLE
        )

        # Extract model configuration
        model_config = specialist_config.get("model", {})
        provider = model_config.get("provider", "openai")
        # Support both "model_name" and "model" field names
        model_name = model_config.get("model") or model_config.get("model_name", "gpt-4.1.mini")

        # Create model instance (avoids deepagents profile forcing use_responses_api=True)
        model_instance = ModelFactory.create_model(
            provider=provider,
            model_name=model_name,
        )

        # Filter tools for this specialist
        tool_names = specialist_config.get("tools", [])
        filtered_tools: List[BaseTool] = []

        missing_from_available: list[str] = []
        for tool_name in tool_names:
            if tool_name in available_tools:
                filtered_tools.append(available_tools[tool_name])
            else:
                missing_from_available.append(tool_name)
                logger.warning(
                    "tool_not_found_for_subagent",
                    agent_name=agent_name,
                    tool_name=tool_name,
                    available_tools=list(available_tools.keys()),
                )

        resolved_tools: list[str] = []
        if missing_from_available and HAS_GITHUB_MIDDLEWARE:
            _GIT_TOOL_MAP = {
                "execute_shell": _git_execute_shell,
                "open_pull_request": _git_open_pull_request,
            }
            for name in missing_from_available:
                tool_fn = _GIT_TOOL_MAP.get(name)
                if tool_fn is not None:
                    filtered_tools.append(tool_fn)
                    resolved_tools.append(name)
                    logger.info(
                        "resolved_middleware_tool",
                        agent_name=agent_name,
                        tool_name=name,
                        source="GitHubMiddleware",
                    )

        if not filtered_tools:
            logger.warning(
                "subagent_has_no_tools", agent_name=agent_name, requested_tools=tool_names
            )

        # Get system prompt
        system_prompt = specialist_config.get("system_prompt", "")
        if not system_prompt:
            logger.warning("subagent_missing_system_prompt", agent_name=agent_name)

        # Extract brief description
        brief_description = specialist_config.get(
            "description",
            system_prompt[:200] + "..." if len(system_prompt) > 200 else system_prompt,
        )

        # Extract response_format (ToolStrategy schema dict or raw dict)
        response_format = specialist_config.get("response_format")

        logger.info(
            "subagent_description_extracted",
            agent_name=agent_name,
            has_description=bool(specialist_config.get("description")),
            description_length=len(brief_description),
            description_preview=brief_description[:100] if brief_description else "EMPTY",
            has_response_format=response_format is not None,
        )

        # Check if state_schema or rubric is defined
        has_state_schema = "state_schema" in specialist_config
        has_rubric = "rubric" in specialist_config

        if (has_state_schema or has_rubric) and DEEPAGENTS_AVAILABLE:
            # PATH A: Create CompiledSubAgent with custom state schema / rubric
            return _build_compiled_subagent(
                agent_name,
                model_instance,
                system_prompt,
                filtered_tools,
                specialist_config,
                brief_description,
                response_format,
            )
        else:
            # PATH B: Return SubAgent dict (let SubAgentMiddleware handle it)
            # NOTE(v2): SubAgent dict path — interrupt_on is inherited from
            #            create_deep_agent's top-level interrupt_on parameter.
            return _build_subagent_dict(
                agent_name,
                model_instance,
                system_prompt,
                filtered_tools,
                brief_description,
                response_format,
            )

    except Exception as e:
        logger.error(
            "subagent_compilation_failed",
            agent_name=agent_name,
            error=str(e),
            error_type=type(e).__name__,
        )
        raise SubAgentCompilationError(f"Failed to compile sub-agent '{agent_name}': {e}") from e


def _build_compiled_subagent(
    agent_name: str,
    model_instance: Any,
    system_prompt: str,
    filtered_tools: List[BaseTool],
    specialist_config: Dict[str, Any],
    brief_description: str,
    response_format: Any = None,
) -> Any:  # Returns CompiledSubAgent
    """Build CompiledSubAgent with optional state schema and filesystem middleware."""

    # Create state schema from config if provided
    state_schema_config = specialist_config.get("state_schema")
    if state_schema_config:
        state_schema_class = create_state_schema_from_config(state_schema_config)  # type: ignore
        logger.info(
            "building_compiled_subagent",
            agent_name=agent_name,
            state_fields=list(state_schema_config.keys()),
        )
    else:
        state_schema_class = None
        logger.info("building_compiled_subagent", agent_name=agent_name, state_fields=[])

    middleware_stack = []

    # Prepend rubric middleware if configured
    rubric_config = specialist_config.get("rubric")
    if rubric_config:
        rubric_middlewares = build_rubric_middlewares(rubric_config, model_instance)
        middleware_stack.extend(rubric_middlewares)

    middleware_stack.extend(
        [
            FilesystemMiddleware(),  # type: ignore
            HumanInteractionMiddleware(),  # type: ignore
            PatchToolCallsMiddleware(),  # type: ignore
        ]
    )
    if HAS_GITHUB_MIDDLEWARE:
        tool_names = specialist_config.get("tools", [])
        if any(t in tool_names for t in ("execute_shell", "open_pull_request")):
            middleware_stack.append(GitHubMiddleware())  # type: ignore
            logger.info(
                "added_github_middleware",
                agent_name=agent_name,
            )
    if specialist_config.get("interrupt_on"):
        middleware_stack.append(
            HumanInTheLoopMiddleware(interrupt_on=specialist_config["interrupt_on"])
        )

    create_agent_kwargs: dict[str, Any] = {
        "model": model_instance,
        "system_prompt": system_prompt,
        "tools": filtered_tools,
        "middleware": middleware_stack,
    }
    if state_schema_class is not None:
        create_agent_kwargs["context_schema"] = state_schema_class
    if response_format is not None:
        create_agent_kwargs["response_format"] = response_format

    subagent_runnable = create_agent(**create_agent_kwargs)  # type: ignore

    # Wrap in CompiledSubAgent
    compiled_subagent = CompiledSubAgent(  # type: ignore
        name=agent_name,
        description=brief_description,
        runnable=subagent_runnable,
    )

    logger.info(
        "subagent_compiled_successfully",
        agent_name=agent_name,
        model_identifier=str(model_instance),
        tool_count=len(filtered_tools),
        tool_names=[getattr(t, "name", str(t)) for t in filtered_tools],
        has_state_schema=bool(state_schema_config),
        return_type="CompiledSubAgent",
    )

    return compiled_subagent


def _build_subagent_dict(
    agent_name: str,
    model_instance: Any,
    system_prompt: str,
    filtered_tools: List[BaseTool],
    brief_description: str,
    response_format: Any = None,
) -> Dict[str, Any]:
    """Build SubAgent dict (for SubAgentMiddleware to process)."""

    logger.info(
        "building_subagent_dict",
        agent_name=agent_name,
        has_response_format=response_format is not None,
    )

    subagent_dict: dict[str, Any] = {
        "name": agent_name,
        "description": brief_description,
        "system_prompt": system_prompt,
        "tools": filtered_tools,
        "model": model_instance,
    }
    if response_format is not None:
        subagent_dict["response_format"] = response_format

    logger.info(
        "subagent_dict_created",
        agent_name=agent_name,
        model_identifier=str(model_instance),
        tool_count=len(filtered_tools),
        tool_names=[getattr(t, "name", str(t)) for t in filtered_tools],
        has_state_schema=False,
        return_type="SubAgent_dict",
    )

    return subagent_dict
