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
        self.model_name: str | None = None
        nodes = agent_definition.get("nodes", [])
        if nodes:
            node_config = nodes[0].get("config", {})
            model_cfg = node_config.get("model", {})
            self.model_name = model_cfg.get("model_name") or model_cfg.get("model")
        if not self.model_name:
            raise ValueError(
                "No model name found in agent definition. "
                "Set config.model.model_name in the first node, "
                "or set LLM_MODEL_NAME env var"
            )
        self.checkpointer = execution_manager.checkpointer

        # Build ArtifactBackend once — reused for tool loading + graph wiring
        if hasattr(self.execution_manager, "_pool") and self.execution_manager._pool is not None:
            from core.backends.artifact import ArtifactBackend

            self._backend = ArtifactBackend(
                workspace_id=self.workspace_id,
                session_id=self.session_id,
                pool=self.execution_manager._pool,
            )

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
        pass
