"""Session lifecycle — manages agent session state and turn execution.

Creates the agent graph on first invocation, persists messages across
turns, and delegates to executor.py for run/stream logic.
"""

import uuid
from typing import Any, Optional

import structlog

from core.event_publisher import EventPublisher
from core.executor import ExecutionManager
from core.factory import build_agent_from_definition
from core.mcp_loader import load_mcp_tools_from_servers

logger = structlog.get_logger(__name__)


class Session:
    def __init__(
        self,
        agent_definition: dict[str, Any],
        input_payload: dict[str, Any],
        execution_manager: ExecutionManager,
        publisher: EventPublisher,
        mcp_servers: Optional[list[dict[str, Any]]] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.session_id = session_id or f"sess_{uuid.uuid4().hex[:24]}"
        self.agent_definition = agent_definition
        self.base_payload = input_payload
        self.execution_manager = execution_manager
        self.publisher = publisher
        self.turns = 0
        self.mcp_servers = mcp_servers or []
        self.mcp_tools: dict[str, Any] = {}
        self.mcp_handles: list[Any] = []
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

    async def initialize_async(self) -> None:
        if self.mcp_servers:
            mcp_tools, handles = await load_mcp_tools_from_servers(self.mcp_servers)
            self.mcp_tools = mcp_tools
            self.mcp_handles = handles
            logger.info(
                "mcp_tools_loaded",
                count=len(mcp_tools),
                servers=[s.get("name") for s in self.mcp_servers],
            )

    def initialize(self, resume_payload: Optional[Any] = None) -> None:
        if resume_payload:
            logger.info("session_resuming", session_id=self.session_id, has_resume=True)
        else:
            logger.info("session_initialized", session_id=self.session_id)
        self.resume_payload = resume_payload

    async def async_run_turn(
        self, user_content: str = "", publisher: Optional[EventPublisher] = None
    ) -> str:
        self.turns += 1

        input_payload = dict(self.base_payload)
        messages = input_payload.get("messages", [])
        if user_content:
            messages = messages + [{"role": "user", "content": user_content}]
        input_payload["messages"] = messages

        compiled_graph = build_agent_from_definition(
            self.agent_definition,
            checkpointer=self.checkpointer,
            extra_tools=self.mcp_tools if self.mcp_tools else None,
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
            extra_tools=self.mcp_tools if self.mcp_tools else None,
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
            extra_tools=self.mcp_tools if self.mcp_tools else None,
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
        for handle in self.mcp_handles:
            await handle.cleanup()
        self.mcp_handles = []
        self.mcp_tools = {}
