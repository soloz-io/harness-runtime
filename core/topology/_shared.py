"""
Shared helpers for composite topology builders (hybrid).

Extracts the orchestrator construction logic used by the star topology
into reusable functions so a hybrid builder (one orchestrator + a mix of
declarative subagents, nested deep agents, and nested subgraphs) can build
its graph without modifying the existing star/acrylic builders.
"""

from pathlib import Path
from typing import Any, Dict, List

import structlog
from langchain_core.runnables import Runnable
from langchain_quickjs import CodeInterpreterMiddleware

from core.middleware.custom_tool_middleware import CustomToolMiddleware
from core.middleware.human_interaction import HumanInteractionMiddleware
from core.middleware.rubric_middleware import build_rubric_middlewares
from core.middleware.shell_middleware import ShellMiddleware
from core.middleware.structured_output import build_tool_strategy, resolve_structured_output_model
from core.middleware.url_fetch_middleware import UrlFetchMiddleware

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
    extra_tool_dirs: list[Path] | None = None,
) -> list[Any]:
    """Replicate the orchestrator middleware stack (rubric → code interp →
    HITL → shell → url fetch → custom tools).

    ShellMiddleware exposes load_skill, which stays unconditional — same
    rationale as subagent_builder.py's star-topology stack: every specialist
    that declares config.skills expects load_skill to be a real tool, not
    deepagents' own skills=[...] kwarg's read_file-based convention (which
    create_deep_agent also wires in when skills are configured — the two
    coexist; a specialist's own prompt is free to use either).

    ``extra_tool_dirs`` (typically a skill's own ``scripts/`` directory —
    see ``build_deep_agent_runnable``) is searched by ``run_tool`` alongside
    ``tools_spec``'s node/shared dirs, so a skill can ship an executable
    CLI wrapper (e.g. ``compile_check_cli.py``) without also requiring a
    separate ``agents/<node-id>/tools/`` folder baked into the image.
    """
    rubric_config = config.get("rubric")
    middleware_stack = build_rubric_middlewares(rubric_config, model)
    middleware_stack.append(CodeInterpreterMiddleware(timeout=300))
    logger.info("code_interpreter_middleware_appended")
    middleware_stack.append(HumanInteractionMiddleware())
    middleware_stack.append(ShellMiddleware())
    middleware_stack.append(UrlFetchMiddleware())
    tool_dirs: list[Path] = list(tools_spec.search_dirs) if tools_spec else []
    tool_dirs.extend(extra_tool_dirs or [])
    if tool_dirs:
        middleware_stack.append(CustomToolMiddleware(tool_dirs))
        logger.info(
            "custom_tool_middleware_appended",
            tools_dirs=[str(d) for d in tool_dirs],
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
    # Deliberately NOT passed to create_deep_agent as skills=[...]: that wires
    # deepagents' SkillsMiddleware, whose SKILLS_SYSTEM_PROMPT instructs the
    # model to read skills with `read_file(file_path=..., limit=1000)`. That
    # directly contradicts agents/shared/skill-contracts.md, which is baked
    # into every specialist's prompt and mandates `load_skill(skill_name=...)`
    # (ShellMiddleware) with BLOCK-on-error semantics. Given both, the model
    # follows the deepagents prompt — it is more concrete and ships a worked
    # example — and load_skill goes unused, which was observed in a real
    # session. Skill names and descriptions already reach the model via each
    # agent's own <agent_skills> section, so SkillsMiddleware's listing is
    # redundant here too. load_skill resolves names to paths itself.
    node_skills = config.get("skills") or skills
    if node_skills:
        logger.info("skills_available_via_load_skill", skills=node_skills)

    # A skill's own scripts/ dir (e.g. workflow-preview's compile_check_cli.py,
    # shipped alongside compile-preview.js) is dispatchable via run_tool once
    # SkillsManager has symlinked the skill onto real disk — checked here,
    # not assumed, since it depends on skill isolation having already run.
    skill_tool_dirs = [
        scripts_dir
        for skill_path in (node_skills or [])
        if (scripts_dir := Path(skill_path) / "scripts").is_dir()
    ]

    middleware_stack = build_middleware_stack(
        config,
        deep_agent_kwargs["model"],
        tools_spec=(tools_ctx.node_tools.get(node_id) if tools_ctx and node_id else None),
        extra_tool_dirs=skill_tool_dirs,
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
