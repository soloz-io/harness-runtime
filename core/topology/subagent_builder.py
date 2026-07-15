"""
Subagent Builder Module for Agent Executor.

This module provides functionality for building SubAgent specs (declarative
subagent definitions) from specialist configuration. The specs are consumed
by the orchestrator's create_deep_agent, which compiles them via create_agent
and registers them as task-tool delegate targets.

Functions:
    - build_subagent: Build a SubAgent spec from specialist configuration

References:
    - Requirements: Req. 3.1 (Stateful Graph Execution)
    - Design: Section 3.2.2 (Sub-Agent Compilation)
    - Spec: build_agent_from_definition.md (create_compiled_subagent)
"""

from typing import Any, Dict, List

import structlog
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.tools import BaseTool

from core.middleware.human_interaction import HumanInteractionMiddleware
from core.middleware.rubric_middleware import build_rubric_middlewares
from core.middleware.shell_middleware import ShellMiddleware
from core.model_factory import ModelFactory
from core.state_schema_builder import create_state_schema_from_config

# ShellMiddleware provides execute_shell for all subagents.  This runs
# in the sandboxed pod with no additional isolation beyond the pod
# boundary.  GitHubMiddleware (open_pull_request) is omitted here
# because PR creation is scoped to specific agent definitions.

logger = structlog.get_logger(__name__)


class SubAgentCompilationError(Exception):
    """Raised when sub-agent compilation fails."""

    pass


def build_subagent(
    specialist_config: Dict[str, Any],
    available_tools: Dict[str, BaseTool],
    *,
    skills: list[str] | None = None,
) -> dict[str, Any]:  # Returns SubAgent dict
    """
    Build a SubAgent spec from specialist configuration.

    Returns a declarative SubAgent dict consumed by the orchestrator's
    create_deep_agent. The orchestrator's create_deep_agent compiles it
    via create_agent (not create_deep_agent) and registers it as a
    task-tool delegate target.

    Args:
        specialist_config: Configuration dictionary containing:
            - name: Sub-agent identifier (string)
            - model: Model configuration (dict with provider, model_name)
            - system_prompt: Instructions for the agent (string)
            - tools: List of tool names this agent can use (list of strings)
            - description: Optional brief description
        available_tools: Dictionary of all loaded tools (from load_tools_from_definition)

    Returns:
        SubAgent dict

    Raises:
        SubAgentCompilationError: If sub-agent spec building fails

    References:
        - Requirements: Req. 3.1, 14.2
        - Design: Section 3.2.2 (Sub-Agent Compilation)
        - Spec: build_agent_from_definition.md (create_compiled_subagent)
        - Tasks: Task 1.1
    """
    agent_name = specialist_config.get("name", "unnamed_agent")

    try:
        logger.info("building_subagent", agent_name=agent_name)

        # Extract model configuration
        model_config = specialist_config.get("model", {})
        provider = model_config.get("provider", "openai")
        model_name = model_config.get("model") or model_config.get("model_name", "gpt-4.1.mini")

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

        if not filtered_tools:
            logger.warning(
                "subagent_has_no_tools", agent_name=agent_name, requested_tools=tool_names
            )

        system_prompt = specialist_config.get("system_prompt", "")
        if not system_prompt:
            logger.warning("subagent_missing_system_prompt", agent_name=agent_name)

        brief_description = specialist_config.get(
            "description",
            system_prompt[:200] + "..." if len(system_prompt) > 200 else system_prompt,
        )

        response_format = specialist_config.get("response_format")

        logger.info(
            "subagent_description_extracted",
            agent_name=agent_name,
            has_description=bool(specialist_config.get("description")),
            description_length=len(brief_description),
            description_preview=brief_description[:100] if brief_description else "EMPTY",
            has_response_format=response_format is not None,
        )

        return _build_subagent_spec(
            agent_name,
            model_instance,
            system_prompt,
            filtered_tools,
            specialist_config,
            brief_description,
            response_format,
            skills=skills,
        )

    except Exception as e:
        logger.error(
            "subagent_compilation_failed",
            agent_name=agent_name,
            error=str(e),
            error_type=type(e).__name__,
        )
        raise SubAgentCompilationError(f"Failed to build sub-agent spec '{agent_name}': {e}") from e


def _build_subagent_spec(
    agent_name: str,
    model_instance: Any,
    system_prompt: str,
    filtered_tools: List[BaseTool],
    specialist_config: Dict[str, Any],
    brief_description: str,
    response_format: Any = None,
    *,
    skills: list[str] | None = None,
) -> dict[str, Any]:
    """Build a declarative SubAgent dict.

    Returns a SubAgent spec consumed by the orchestrator's create_deep_agent.
    create_deep_agent compiles it via create_agent and auto-adds
    TodoListMiddleware, FilesystemMiddleware, SummarizationMiddleware,
    PatchToolCallsMiddleware, and SkillsMiddleware (if skills are set).
    This function only provides middleware that create_deep_agent does not:
    RubricMiddleware, HumanInteractionMiddleware, ShellMiddleware.
    """

    state_schema_config = specialist_config.get("state_schema")
    if state_schema_config:
        state_schema_class = create_state_schema_from_config(state_schema_config)
        logger.info(
            "building_subagent_spec",
            agent_name=agent_name,
            state_fields=list(state_schema_config.keys()),
        )
    else:
        state_schema_class = None
        logger.info("building_subagent_spec", agent_name=agent_name, state_fields=[])

    middleware_stack = []

    rubric_config = specialist_config.get("rubric")
    if rubric_config:
        rubric_middlewares = build_rubric_middlewares(rubric_config, model_instance)
        middleware_stack.extend(rubric_middlewares)

    middleware_stack.append(HumanInteractionMiddleware())
    middleware_stack.append(ShellMiddleware())

    if specialist_config.get("interrupt_on"):
        middleware_stack.append(
            HumanInTheLoopMiddleware(interrupt_on=specialist_config["interrupt_on"])
        )

    subagent_spec: dict[str, Any] = {
        "name": agent_name,
        "description": brief_description,
        "system_prompt": system_prompt,
        "model": model_instance,
        "tools": filtered_tools,
        "middleware": middleware_stack,
    }

    subagent_skills = skills or specialist_config.get("skills")
    if subagent_skills:
        from deepagents.middleware.permissions import FilesystemPermission

        allowed = [f"{s.rstrip('/')}/**" for s in subagent_skills]
        subagent_spec["permissions"] = [
            FilesystemPermission(operations=["read"], paths=allowed, mode="allow"),
            FilesystemPermission(
                operations=["read", "write"],
                paths=["/workspace/.builder/scratch/**"],
                mode="allow",
            ),
            FilesystemPermission(
                operations=["read"], paths=["/workspace/.builder/skills/*/**"], mode="deny"
            ),
        ]

        subagent_spec["skills"] = [f"/workspace/.builder/agent/{agent_name}/"]

    if state_schema_class is not None:
        subagent_spec["context_schema"] = state_schema_class
    if response_format is not None:
        subagent_spec["response_format"] = response_format

    logger.info(
        "subagent_spec_built",
        agent_name=agent_name,
        model_identifier=str(model_instance),
        tool_count=len(filtered_tools),
        tool_names=[getattr(t, "name", str(t)) for t in filtered_tools],
        has_state_schema=bool(state_schema_config),
        has_skills=bool(subagent_skills),
        return_type="SubAgent",
    )

    return subagent_spec
