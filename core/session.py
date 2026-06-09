import uuid
from typing import Any

import structlog

from core.builder import GraphBuilder
from core.event_publisher import EventPublisher
from core.executor import ExecutionManager

logger = structlog.get_logger(__name__)


class Session:
    def __init__(
        self,
        agent_definition: dict[str, Any],
        input_payload: dict[str, Any],
        execution_manager: ExecutionManager,
        publisher: EventPublisher,
    ) -> None:
        self.session_id = f"sess_{uuid.uuid4().hex[:24]}"
        self.agent_definition = agent_definition
        self.base_payload = input_payload
        self.execution_manager = execution_manager
        self.publisher = publisher
        self.turns = 0
        self.model_name: str | None = None
        nodes = agent_definition.get("nodes", [])
        if nodes:
            node_config = nodes[0].get("config", {})
            model_cfg = node_config.get("model", {})
            self.model_name = (
                model_cfg.get("model_name")
                or model_cfg.get("model")
            )
        if not self.model_name:
            raise ValueError(
                "No model name found in agent definition. "
                "Set config.model.model_name in the first node, "
                "or set LLM_MODEL_NAME env var"
            )
        self.graph_builder = GraphBuilder(
            checkpointer=execution_manager.checkpointer
        )

    def initialize(self) -> None:
        logger.info("session_initialized", session_id=self.session_id)

    def run_turn(self, user_content: str = "") -> str:
        self.turns += 1

        input_payload = dict(self.base_payload)
        messages = input_payload.get("messages", [])
        if user_content:
            messages = messages + [{"role": "user", "content": user_content}]
        input_payload["messages"] = messages

        compiled_graph = self.graph_builder.build_from_definition(
            self.agent_definition
        )

        result = self.execution_manager.execute(
            graph=compiled_graph,
            session_id=self.session_id,
            input_payload=input_payload,
            model_name=self.model_name,
            agent_definition=self.agent_definition,
            num_turns=self.turns,
        )

        return result
