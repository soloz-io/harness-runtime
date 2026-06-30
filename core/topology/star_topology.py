"""
Star Topology Builder.

Compiles a native LangGraph StateGraph from definition JSON edges,
using deepagents create_deep_agent (star topology with an orchestrator).
"""

from typing import Any, Dict, List

import structlog
from langchain_core.runnables import Runnable

from core.interfaces import TopologyBuilder
from core.middleware.human_interaction import HumanInteractionMiddleware
from core.middleware.rubric_middleware import build_rubric_middlewares
from core.middleware.structured_output import build_tool_strategy, resolve_structured_output_model
from core.topology.subagent_builder import build_subagent

try:
    from deepagents import create_deep_agent
except ImportError as e:
    raise ImportError(
        "deepagents package is required but not installed. "
        "Install it with: pip install deepagents>=0.2.0"
    ) from e

try:
    from deepagents.backends import CompositeBackend, StateBackend
except ImportError:
    CompositeBackend = None  # type: ignore[assignment,misc]
    StateBackend = None  # type: ignore[assignment,misc]

try:
    from deepagents.backends.filesystem import FilesystemBackend
except ImportError:
    FilesystemBackend = None  # type: ignore[assignment,misc]

try:
    from core.integration.git_backend import GitBackend
except ImportError:
    GitBackend = None  # type: ignore[assignment,misc]

logger = structlog.get_logger(__name__)


class StarTopologyBuilder(TopologyBuilder):
    """Builds a star topology using an orchestrator and subagents."""

    def build(
        self,
        definition: Dict[str, Any],
        available_tools: Dict[str, Any],
        checkpointer: Any,
    ) -> Runnable[Any, Any]:
        """Build the start topology graph."""
        nodes = definition.get("nodes", [])
        if not nodes:
            raise ValueError("Agent definition must contain at least one node")

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
            specialist_count=len(specialist_configs),
        )

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

        orchestrator_actual_config = orchestrator_config.get("config", {})

        orchestrator_model_config = orchestrator_actual_config.get("model", {})
        orchestrator_provider = orchestrator_model_config.get("provider", "openai")
        orchestrator_model_name = orchestrator_model_config.get(
            "model_name"
        ) or orchestrator_model_config.get("model")
        if not orchestrator_model_name:
            raise ValueError(
                "Agent definition must specify a model "
                "(add config.model.model_name to the orchestrator node)"
            )

        orchestrator_system_prompt = orchestrator_actual_config.get("system_prompt", "")

        orchestrator_response_format_raw = orchestrator_actual_config.get("response_format")
        orchestrator_response_format = build_tool_strategy(orchestrator_response_format_raw)
        orchestrator_state_schema = orchestrator_actual_config.get("state_schema")
        orchestrator_context_schema = orchestrator_actual_config.get("context_schema")

        orchestrator_tool_names = orchestrator_actual_config.get("tools", [])
        orchestrator_tools = []

        for tool_name in orchestrator_tool_names:
            if tool_name in available_tools:
                orchestrator_tools.append(available_tools[tool_name])
            else:
                logger.warning(
                    "orchestrator_tool_not_found",
                    tool_name=tool_name,
                    available_tools=list(available_tools.keys()),
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

        interrupt_on_config = orchestrator_actual_config.get("interrupt_on")
        rubric_config = orchestrator_actual_config.get("rubric")

        git_ref = orchestrator_actual_config.get("gitRef")

        deep_agent_kwargs: dict[str, Any] = {
            "model": resolve_structured_output_model(
                provider=orchestrator_provider,
                model_name=orchestrator_model_name,
                response_format=orchestrator_response_format_raw,
            ),
            "system_prompt": orchestrator_system_prompt,
            "tools": orchestrator_tools,
            "subagents": compiled_subagents,
            "checkpointer": checkpointer,
            "debug": True,
        }

        # Build middlewares starting with Rubric if configured
        middleware_stack = build_rubric_middlewares(rubric_config, deep_agent_kwargs["model"])
        middleware_stack.append(HumanInteractionMiddleware())
        if middleware_stack:
            deep_agent_kwargs["middleware"] = middleware_stack

        if interrupt_on_config:
            deep_agent_kwargs["interrupt_on"] = interrupt_on_config
        if orchestrator_response_format is not None:
            deep_agent_kwargs["response_format"] = orchestrator_response_format
        if orchestrator_state_schema is not None:
            deep_agent_kwargs["state_schema"] = orchestrator_state_schema
        if orchestrator_context_schema is not None:
            deep_agent_kwargs["context_schema"] = orchestrator_context_schema

        # ── Git-backed skills ─────────────────────────────────────────────
        if git_ref:
            logger.info("git_ref_present", git_ref=git_ref)

            if GitBackend is None:
                raise ImportError(
                    "gitRef requires GitBackend — core.integration.git_backend not available"
                )
            if CompositeBackend is None or StateBackend is None:
                raise ImportError("gitRef requires deepagents >= 0.6.8")
            if FilesystemBackend is None:
                raise ImportError(
                    "gitRef requires deepagents.backends.filesystem.FilesystemBackend"
                )

            logger.info("git_ref_start_clone", git_ref=git_ref)
            cloner = GitBackend(git_ref)
            logger.info(
                "git_ref_clone_done",
                git_ref=git_ref,
                local_path=str(cloner.path),
            )

            logger.info(
                "git_ref_create_filesystem_backend", root_dir=str(cloner.path), virtual_mode=True
            )
            FilesystemBackend(
                root_dir=str(cloner.repo_path / "skills"),
                virtual_mode=True,
            )
            FilesystemBackend(
                root_dir=str(cloner.repo_path / "memory"),
                virtual_mode=True,
            )
            workspace_backend = FilesystemBackend(
                root_dir=str(cloner.repo_path / "workspace"),
                virtual_mode=True,
            )

            logger.info(
                "git_ref_create_composite_backend",
                route="/skills/, /memory/, and /workspace/",
                default_backend="StateBackend",
            )
            composite_backend = CompositeBackend(
                default=StateBackend(),
                routes={
                    "/workspace/skills/": workspace_backend,
                    "/workspace/memory/": workspace_backend,
                },
            )
            # SkillsMiddleware is auto-wired by create_deep_agent when
            # both skills=["/skills/"] and backend=composite_backend are passed.
            logger.info(
                "git_ref_wire_deep_agent_kwargs",
                backend_type=type(composite_backend).__name__,
                skills=["/skills/"],
            )
            deep_agent_kwargs["backend"] = composite_backend
            deep_agent_kwargs["skills"] = ["/skills/"]
            deep_agent_kwargs["memory"] = ["/memory/"]

        else:
            logger.info("git_ref_absent_no_git_skills")

        logger.info(
            "create_deep_agent_start",
            has_backend="backend" in deep_agent_kwargs,
            has_skills="skills" in deep_agent_kwargs,
            skills=deep_agent_kwargs.get("skills"),
        )
        main_runnable = create_deep_agent(**deep_agent_kwargs)

        logger.info(
            "create_deep_agent_result",
            runnable_type=type(main_runnable).__name__,
            has_nodes=hasattr(main_runnable, "nodes"),
        )

        logger.info(
            "graph_built_successfully",
            orchestrator_name=orchestrator_actual_config.get("name", "main"),
            sub_agent_count=len(compiled_subagents),
            total_tools=len(available_tools),
            graph_type="deep_agent",
            skills_configured=bool(git_ref),
        )

        return main_runnable
