"""Session lifecycle — manages agent session state and turn execution.

Thin orchestrator that delegates to focused sub-modules:
- ``config``: agent configuration extraction and persistence
- ``backends``: Backend construction (DBBackend or S3Backend)
- ``skills``: SkillsManager for skills lifecycle
- ``tools``: ToolsManager for CLI tools lifecycle
- ``execution``: graph construction, input preparation, turn helpers
"""

import asyncio
from typing import Any, Optional

import structlog

from core.agent_backend import is_s3_mode, resolve_backend
from core.event_publisher import EventPublisher
from core.executor import ExecutionManager
from core.metro import ensure_metro_running, ensure_watcher_running
from core.session.config import AgentConfig, extract_agent_config, persist_system_prompt
from core.session.execution import (
    build_graph,
    consume_resume,
    initialize_tool_registry,
    inline_object_attachments,
    prepare_turn_input,
)
from core.session.skill_paths import normalize_agent_definition
from core.session.skills import SkillsManager
from core.session.tools import ToolsManager
from core.workspace_context import set_active_context

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
        app_id: Optional[str] = None,
    ) -> None:
        from uuid import uuid4

        self.session_id = session_id or f"sess_{uuid4().hex[:24]}"
        self.workspace_id = workspace_id
        self.app_id = app_id
        # Normalize skill paths to the runtime backend root (/workspace/.builder/skills/...)
        # so LLM-visible paths match the FilesystemBackend routes and symlinks.
        self.agent_definition = normalize_agent_definition(agent_definition)
        self.base_payload = input_payload
        self.execution_manager = execution_manager
        self.publisher = publisher
        self.turns = 0

        if not workspace_id:
            raise ValueError("workspace_id is required")

        # The workspace arrives ready. Nothing to prepare here.
        #
        # This used to call ensure_workspace_repo() to git-init /workspace, and
        # before that restore_workspace_from_s3() to populate it. Both are gone:
        # the platform's workspace-sync sidecar mounts the workspace before this
        # process starts (zero-ops ADR-052 §14.2), and git is no longer the
        # source of truth for its contents.
        #
        # The harness now has no knowledge of how /workspace came to exist —
        # which is the whole point of the split (waypoint ADR-036 §10). It reads
        # and writes a directory.
        if is_s3_mode(self.agent_definition):
            set_active_context(
                workspace_id, app_id, self.session_id, getattr(execution_manager, "_pool", None)
            )
            try:
                asyncio.create_task(ensure_metro_running())
                ensure_watcher_running()
            except RuntimeError:
                # No running event loop (e.g. constructed outside an async
                # context, such as a sync test) — Metro/HMR is best-effort
                # infrastructure, not a hard dependency of session creation.
                logger.debug("metro_supervision_skipped_no_event_loop")

        # 1. Agent configuration
        cfg: AgentConfig = extract_agent_config(self.agent_definition)
        self.model_name = cfg.model_name
        self.checkpointer = execution_manager.checkpointer
        persist_system_prompt(self.session_id, cfg, getattr(execution_manager, "_pool", None))

        # 2. Backend (DBBackend for "db" mode, S3Backend for "s3" mode)
        self._backend = resolve_backend(
            self.agent_definition,
            workspace_id,
            self.session_id,
            getattr(execution_manager, "_pool", None),
            app_id=app_id,
        )

        # 3. Skills (git clone, temp dirs, FilesystemBackend routes, CompositeBackend)
        self._skills_mgr = SkillsManager(self.agent_definition, self._backend)
        self._skills_ctx = self._skills_mgr.initialize()

        # 4. CLI tools (image-baked tools/<node-id>/ for run_tool dispatch)
        self._tools_mgr = ToolsManager(self.agent_definition)
        self._tools_ctx = self._tools_mgr.initialize()

        # 5. Tool registry (lazy — populated on first turn)
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
        self,
        user_content: str = "",
        publisher: Optional[EventPublisher] = None,
        role: str = "user",
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        self._ensure_initialized()
        self.turns += 1

        attachments = await inline_object_attachments(attachments)
        payload = prepare_turn_input(self.base_payload, user_content, role, attachments)
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
            workspace_id=self.workspace_id,
            app_id=self.app_id,
        )

        # No checkpoint call here, deliberately.
        #
        # Workspace durability is the platform's, end to end (ADR-052 §14): the
        # PVC holds the live tree and the workspace-sync sidecar snapshots it on
        # its own schedule and at teardown. A turn-boundary trigger from this
        # process would mean the agent runtime knows a persistence layer exists,
        # which is the coupling the whole split exists to remove — and it buys
        # nothing durability-wise, because the periodic and teardown snapshots
        # already bound what a crash can lose.
        #
        # Semantic, user-addressable history is git, which the agent DOES own
        # and which lives on the volume like any other file.
        return result

    def run_turn(self, user_content: str = "", role: str = "user") -> str:
        self._ensure_initialized()
        self.turns += 1

        payload = prepare_turn_input(self.base_payload, user_content, role)
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
            workspace_id=self.workspace_id,
            app_id=self.app_id,
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
            workspace_id=self.workspace_id,
            app_id=self.app_id,
        )
        return result

    async def cleanup(self) -> None:
        self._skills_mgr.cleanup()
        self._tools_mgr.cleanup()

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
            tools_ctx=self._tools_ctx,
        )
