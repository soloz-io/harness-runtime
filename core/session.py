"""Session lifecycle — manages agent session state and turn execution.

Creates the agent graph on first invocation, persists messages across
turns, and delegates to executor.py for run/stream logic.
"""

import uuid
from typing import Any, Optional

import structlog

from core.embedded_tool_loader import ToolLoadingError, load_tool_implementations
from core.event_publisher import EventPublisher
from core.executor import ExecutionManager
from core.factory import build_agent_from_definition
from core.tool_registry import ToolRegistry

logger = structlog.get_logger(__name__)


class Session:
    def __init__(
        self,
        agent_definition: dict[str, Any],
        input_payload: dict[str, Any],
        execution_manager: ExecutionManager,
        publisher: EventPublisher,
        session_id: Optional[str] = None,
        workspace_id: str = "",
    ) -> None:
        self.session_id = session_id or f"sess_{uuid.uuid4().hex[:24]}"
        self.workspace_id = workspace_id
        self.agent_definition = agent_definition
        self.base_payload = input_payload
        self.execution_manager = execution_manager
        self.publisher = publisher
        self.turns = 0
        if not workspace_id:
            raise ValueError("workspace_id is required")
        self._tool_registry: ToolRegistry | None = None
        self._initialized = False
        self._backend: Any = None
        self._composite_backend: Any = None
        self._skill_git_backend: Any = None
        model_name: str | None = None
        nodes = agent_definition.get("nodes", [])
        if nodes:
            node_config = nodes[0].get("config", {})
            model_cfg = node_config.get("model", {})
            model_name = model_cfg.get("model_name") or model_cfg.get("model")
        if not model_name:
            raise ValueError(
                "No model name found in agent definition. "
                "Set config.model.model_name in the first node, "
                "or set LLM_MODEL_NAME env var"
            )
        self.model_name: str = model_name
        self.checkpointer = execution_manager.checkpointer

        # Build ArtifactBackend once — reused for tool loading + graph wiring
        self._backend = None
        if hasattr(self.execution_manager, "_pool") and self.execution_manager._pool is not None:
            from core.backends.artifact import ArtifactBackend

            self._backend = ArtifactBackend(
                workspace_id=self.workspace_id,
                session_id=self.session_id,
                pool=self.execution_manager._pool,
            )

        # Initialize skills backend (CompositeBackend wrapping ArtifactBackend + FilesystemBackend)
        self._composite_backend = None
        self._skill_git_backend = None
        self._init_skills()

    def _init_skills(self) -> None:
        """Clone skills repo and build CompositeBackend for skills middleware."""
        skills_paths: list[str] = []
        for node in self.agent_definition.get("nodes", []):
            node_skills = node.get("config", {}).get("skills", [])
            skills_paths.extend(node_skills)

        if not skills_paths:
            logger.info("no_skills_defined_in_agent_definition")
            return

        from core.integration.git_backend import GitBackend

        gb = GitBackend("packages/master-chief-agent/src/skills")
        self._skill_git_backend = gb

        # Symlink wpt-engine CLI into each skill directory
        cli_src = gb.repo_path / "packages" / "wpt-engine" / "build" / "cli.cjs"
        if cli_src.exists():
            for skill_dir in gb.path.iterdir():
                if skill_dir.is_dir():
                    engine_dir = skill_dir / "engine"
                    engine_dir.mkdir(parents=True, exist_ok=True)
                    symlink_target = engine_dir / "cli.cjs"
                    if not symlink_target.exists():
                        symlink_target.symlink_to(cli_src)
                    logger.info(
                        "skills_cli_symlinked",
                        skill=str(skill_dir.name),
                        target=str(symlink_target),
                    )

        # Build CompositeBackend for skills
        # default=ArtifactBackend ensures ALL agents always have DB-backed file access
        # routes provide filesystem access to skill files for agents that declare skills
        try:
            from deepagents.backends.composite import CompositeBackend
            from deepagents.backends.filesystem import FilesystemBackend

            fs_backend = FilesystemBackend(root_dir=str(gb.path), virtual_mode=True)
            self._composite_backend = CompositeBackend(
                default=self._backend,
                routes={"/workspace/.builder/skills/": fs_backend},
            )
            logger.info("composite_backend_built", path=str(gb.path))
        except ImportError:
            logger.warning("composite_backend_failed_deepagents_not_available")

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return

        self._tool_registry = ToolRegistry()

        tool_definitions = self.agent_definition.get("tool_definitions", [])
        if tool_definitions:
            try:
                load_tool_implementations(tool_definitions, self._tool_registry)
            except ToolLoadingError as e:
                logger.error("tool_loading_failed", error=str(e))
                raise

        self._initialized = True

    def initialize(self, resume_payload: Optional[Any] = None) -> None:
        if resume_payload:
            logger.info("session_resuming", session_id=self.session_id, has_resume=True)
        else:
            logger.info("session_initialized", session_id=self.session_id)
        self.resume_payload = resume_payload

    async def async_run_turn(
        self, user_content: str = "", publisher: Optional[EventPublisher] = None
    ) -> str:
        await self._ensure_initialized()

        self.turns += 1

        input_payload = dict(self.base_payload)
        messages = input_payload.get("messages", [])
        if user_content:
            messages = messages + [{"role": "user", "content": user_content}]
        input_payload["messages"] = messages

        compiled_graph = build_agent_from_definition(
            self.agent_definition,
            checkpointer=self.checkpointer,
            tool_registry=self._tool_registry,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            backend=self._backend,
            composite_backend=self._composite_backend,
        )

        resume = getattr(self, "resume_payload", None)
        result = await self.execution_manager.async_execute(
            graph=compiled_graph,
            session_id=self.session_id,
            input_payload=input_payload,
            model_name=self.model_name,
            publisher=publisher or self.publisher,
            agent_definition=self.agent_definition,
            num_turns=self.turns,
            resume_payload=resume,
        )

        return result

    def run_turn(self, user_content: str = "") -> str:
        self.turns += 1

        input_payload = dict(self.base_payload)
        messages = input_payload.get("messages", [])
        if user_content:
            messages = messages + [{"role": "user", "content": user_content}]
        input_payload["messages"] = messages

        compiled_graph = build_agent_from_definition(
            self.agent_definition,
            checkpointer=self.checkpointer,
            tool_registry=self._tool_registry,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            backend=self._backend,
            composite_backend=self._composite_backend,
        )

        resume = getattr(self, "resume_payload", None)
        result = self.execution_manager.execute(
            graph=compiled_graph,
            session_id=self.session_id,
            input_payload=input_payload,
            model_name=self.model_name,
            agent_definition=self.agent_definition,
            num_turns=self.turns,
            resume_payload=resume,
        )

        return result

    def resume_turn(self, resume_payload: Any) -> str:
        self.turns += 1
        compiled_graph = build_agent_from_definition(
            self.agent_definition,
            checkpointer=self.checkpointer,
            tool_registry=self._tool_registry,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            backend=self._backend,
            composite_backend=self._composite_backend,
        )
        result = self.execution_manager.execute(
            graph=compiled_graph,
            session_id=self.session_id,
            input_payload={},
            model_name=self.model_name,
            agent_definition=self.agent_definition,
            num_turns=self.turns,
            resume_payload=resume_payload,
        )
        return result

    async def cleanup(self) -> None:
        if self._skill_git_backend is not None:
            self._skill_git_backend.cleanup()
