"""
Shared helpers for composite topology builders (hybrid).

Extracts the orchestrator construction logic used by the star topology
into reusable functions so a hybrid builder (one orchestrator + a mix of
declarative subagents, nested deep agents, and nested subgraphs) can build
its graph without modifying the existing star/acrylic builders.
"""

from typing import Any, Dict, List

import structlog
from langchain_core.runnables import Runnable
from langchain_quickjs import CodeInterpreterMiddleware

from core.middleware.custom_tool_middleware import CustomToolMiddleware
from core.middleware.human_interaction import HumanInteractionMiddleware
from core.middleware.rubric_middleware import build_rubric_middlewares
from core.middleware.structured_output import build_tool_strategy, resolve_structured_output_model

logger = structlog.get_logger(__name__)

try:
    from deepagents import create_deep_agent
except ImportError as e:
    raise ImportError(
        "deepagents package is required but not installed. "
        "Install it with: pip install deepagents>=0.2.0"
    ) from e


def ensure_artifact_backend(
    backend: Any,
    *,
    workspace_id: str | None,
    session_id: str | None,
    db_pool: Any,
) -> Any:
    """Auto-construct a SessionArtifactBackend when one isn't pre-built."""
    if backend is None and workspace_id and session_id and db_pool is not None:
        from core.backends.artifact import SessionArtifactBackend

        backend = SessionArtifactBackend(
            workspace_id=workspace_id,
            session_id=session_id,
            pool=db_pool,
        )
        logger.info("session_artifact_backend_auto_constructed")
    return backend


def resolve_tools_from_config(
    config: Dict[str, Any],
    available_tools: Dict[str, Any],
    *,
    owner: str = "node",
) -> List[Any]:
    """Resolve ``config["tools"]`` names against the available tool map."""
    tool_names = config.get("tools", [])
    tools: List[Any] = []
    for tool_name in tool_names:
        if tool_name in available_tools:
            tools.append(available_tools[tool_name])
        else:
            logger.warning(
                "tool_not_found",
                owner=owner,
                tool_name=tool_name,
                available_tools=list(available_tools.keys()),
            )
    return tools


def build_middleware_stack(
    config: Dict[str, Any],
    model: Any,
    *,
    tools_spec: Any = None,
) -> list[Any]:
    """Replicate the orchestrator middleware stack (rubric → code interp →
    HITL → custom tools)."""
    rubric_config = config.get("rubric")
    middleware_stack = build_rubric_middlewares(rubric_config, model)
    middleware_stack.append(CodeInterpreterMiddleware(timeout=300))
    logger.info("code_interpreter_middleware_appended")
    middleware_stack.append(HumanInteractionMiddleware())
    if tools_spec:
        middleware_stack.append(CustomToolMiddleware(tools_spec.search_dirs))
        logger.info(
            "custom_tool_middleware_appended",
            tools_dirs=[str(d) for d in tools_spec.search_dirs],
        )
    return middleware_stack


def build_deep_agent_runnable(
    config: Dict[str, Any],
    available_tools: Dict[str, Any],
    *,
    checkpointer: Any = None,
    node_id: str | None = None,
    subagents: List[Any] | None = None,
    skills: list[str] | None = None,
    composite_backend: Any = None,
    backend: Any = None,
    tools_ctx: Any = None,
) -> Runnable[Any, Any]:
    """Build a ``create_deep_agent()`` runnable from a node config.

    Used for the hybrid orchestrator and for nested ``deepagent``
    specialists.  Mirrors the star topology's orchestrator construction.
    """
    model_config = config.get("model", {})
    provider = model_config.get("provider", "openai")
    model_name = model_config.get("model_name") or model_config.get("model")
    if not model_name:
        raise ValueError(
            "Agent definition must specify a model (add config.model.model_name to the node)"
        )

    system_prompt = config.get("system_prompt", "")
    response_format_raw = config.get("response_format")
    response_format = build_tool_strategy(response_format_raw)
    state_schema = config.get("state_schema")
    context_schema = config.get("context_schema")

    tools = resolve_tools_from_config(config, available_tools, owner=node_id or "node")

    logger.info(
        "deep_agent_config_extracted",
        model=str(model_name),
        system_prompt_length=len(system_prompt),
        requested_tools=config.get("tools", []),
        resolved_tools=len(tools),
        has_response_format=response_format is not None,
        has_state_schema=state_schema is not None,
        has_context_schema=context_schema is not None,
    )

    interrupt_on_config = config.get("interrupt_on")

    deep_agent_kwargs: dict[str, Any] = {
        "model": resolve_structured_output_model(
            provider=provider,
            model_name=model_name,
            response_format=response_format_raw,
        ),
        "system_prompt": system_prompt,
        "tools": tools,
        "checkpointer": checkpointer,
        "debug": True,
    }
    if subagents:
        deep_agent_kwargs["subagents"] = subagents

    # Wire backend + skills so nested agents can resolve skill files.
    if composite_backend is not None:
        deep_agent_kwargs["backend"] = composite_backend
        logger.info("composite_backend_wired")
    elif backend is not None:
        deep_agent_kwargs["backend"] = backend
        logger.info("artifact_backend_wired")
    node_skills = config.get("skills") or skills
    if node_skills:
        deep_agent_kwargs["skills"] = node_skills
        logger.info("skills_wired", skills=node_skills)

    middleware_stack = build_middleware_stack(
        config,
        deep_agent_kwargs["model"],
        tools_spec=(tools_ctx.node_tools.get(node_id) if tools_ctx and node_id else None),
    )
    if middleware_stack:
        deep_agent_kwargs["middleware"] = middleware_stack

    if interrupt_on_config:
        deep_agent_kwargs["interrupt_on"] = interrupt_on_config
    if response_format is not None:
        deep_agent_kwargs["response_format"] = response_format
    if state_schema is not None:
        deep_agent_kwargs["state_schema"] = state_schema
    if context_schema is not None:
        deep_agent_kwargs["context_schema"] = context_schema

    logger.info("create_deep_agent_start", has_backend="backend" in deep_agent_kwargs)
    return create_deep_agent(**deep_agent_kwargs)
