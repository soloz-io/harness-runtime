"""Session lifecycle — manages agent session state and turn execution.

Thin orchestrator that delegates to focused sub-modules:
- ``config``: agent configuration extraction and persistence
- ``backends``: ArtifactBackend construction
- ``skills``: SkillsManager for skills lifecycle
- ``execution``: graph construction, input preparation, turn helpers
"""

from typing import Any, Optional

import structlog

from core.event_publisher import EventPublisher
from core.executor import ExecutionManager
from core.session.backends import build_artifact_backend
from core.session.config import AgentConfig, extract_agent_config, persist_system_prompt
from core.session.execution import (
    build_graph,
    consume_resume,
    initialize_tool_registry,
    prepare_turn_input,
)
from core.session.skills import SkillsManager

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
        from uuid import uuid4

        self.session_id = session_id or f"sess_{uuid4().hex[:24]}"
        self.workspace_id = workspace_id
        self.agent_definition = agent_definition
        self.base_payload = input_payload
        self.execution_manager = execution_manager
        self.publisher = publisher
        self.turns = 0

        if not workspace_id:
            raise ValueError("workspace_id is required")

        # 1. Agent configuration
        cfg: AgentConfig = extract_agent_config(agent_definition)
        self.model_name = cfg.model_name
        self.checkpointer = execution_manager.checkpointer
        persist_system_prompt(self.session_id, cfg, getattr(execution_manager, "_pool", None))

        # 2. Artifact backend (DB-backed storage for tool outputs)
        self._backend = build_artifact_backend(
            workspace_id,
            self.session_id,
            getattr(execution_manager, "_pool", None),
        )

        # 3. Skills (git clone, temp dirs, FilesystemBackend routes, CompositeBackend)
        self._skills_mgr = SkillsManager(agent_definition, self._backend)
        self._skills_ctx = self._skills_mgr.initialize()

        # 4. Tool registry (lazy — populated on first turn)
        self._tool_registry: Optional[Any] = None
        self._initialized = False

    # ── public API ────────────────────────────────────────────────────

    def initialize(self, resume_payload: Optional[Any] = None) -> None:
        if resume_payload:
            logger.info("session_resuming", session_id=self.session_id, has_resume=True)
        else:
            logger.info("session_initialized", session_id=self.session_id)
        self.resume_payload = resume_payload

    async def async_run_turn(
        self, user_content: str = "", publisher: Optional[EventPublisher] = None
    ) -> str:
        self._ensure_initialized()
        self.turns += 1

        payload = prepare_turn_input(self.base_payload, user_content)
        graph = self._build_graph()
        resume = consume_resume(self)

        result = await self.execution_manager.async_execute(
            graph=graph,
            session_id=self.session_id,
            input_payload=payload,
            model_name=self.model_name,
            publisher=publisher or self.publisher,
            agent_definition=self.agent_definition,
            num_turns=self.turns,
            resume_payload=resume,
        )
        return result

    def run_turn(self, user_content: str = "") -> str:
        self._ensure_initialized()
        self.turns += 1

        payload = prepare_turn_input(self.base_payload, user_content)
        graph = self._build_graph()
        resume = consume_resume(self)

        result = self.execution_manager.execute(
            graph=graph,
            session_id=self.session_id,
            input_payload=payload,
            model_name=self.model_name,
            agent_definition=self.agent_definition,
            num_turns=self.turns,
            resume_payload=resume,
        )
        return result

    def resume_turn(self, resume_payload: Any) -> str:
        self.turns += 1
        graph = self._build_graph()

        result = self.execution_manager.execute(
            graph=graph,
            session_id=self.session_id,
            input_payload={},
            model_name=self.model_name,
            agent_definition=self.agent_definition,
            num_turns=self.turns,
            resume_payload=resume_payload,
        )
        return result

    async def cleanup(self) -> None:
        self._skills_mgr.cleanup()

    # ── internal helpers ──────────────────────────────────────────────

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._tool_registry = initialize_tool_registry(self.agent_definition)
        self._initialized = True

    def _build_graph(self) -> Any:
        if self._tool_registry is None:
            raise RuntimeError("Session tool registry not initialized")
        return build_graph(
            agent_definition=self.agent_definition,
            checkpointer=self.checkpointer,
            tool_registry=self._tool_registry,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            backend=self._backend,
            composite_backend=self._skills_ctx.composite_backend if self._skills_ctx else None,
        )
