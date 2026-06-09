"""Model factory for creating LLM instances based on configuration."""

import os
from typing import Any


def _is_mock_mode() -> bool:
    return os.getenv("USE_MOCK_LLM", "false").lower() == "true"


class ModelFactory:
    """Factory for creating LLM model instances."""

    @staticmethod
    def create_model() -> Any:
        if _is_mock_mode():
            return ModelFactory._create_mock_model()
        else:
            return ModelFactory._create_real_model()

    @staticmethod
    def _create_mock_model() -> Any:
        from tests.utils.mock_workflow import get_mock_model_with_event_replay
        return get_mock_model_with_event_replay()

    @staticmethod
    def _create_real_model() -> Any:
        from langchain_openai import ChatOpenAI
        model_name = os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")
        return ChatOpenAI(model=model_name)

    @staticmethod
    def is_mock_mode() -> bool:
        return _is_mock_mode()


class ExecutionStrategy:
    """Base class for execution strategies."""

    def execute_workflow(self, graph_builder: Any, agent_definition: Any,
                         job_id: str, trace_id: str) -> Any:
        raise NotImplementedError


class RealExecutionStrategy(ExecutionStrategy):
    def __init__(self, execution_manager: Any) -> None:
        self.execution_manager = execution_manager

    def execute_workflow(self, graph_builder: Any, agent_definition: Any,
                         job_id: str, trace_id: str) -> Any:
        compiled_graph = graph_builder.build_from_definition(agent_definition)
        input_payload = agent_definition.get("input_payload", {"messages": []})
        result = self.execution_manager.execute(
            graph=compiled_graph,
            session_id=job_id,
            input_payload=input_payload,
            model_name=agent_definition.get("model", "unknown"),
            agent_definition=agent_definition,
        )
        return result


class MockExecutionStrategy(ExecutionStrategy):
    def __init__(self, execution_manager: Any) -> None:
        self.execution_manager = execution_manager

    def execute_workflow(self, graph_builder: Any, agent_definition: Any,
                         job_id: str, trace_id: str) -> Any:
        from tests.utils.mock_workflow import handle_mock_execution
        return handle_mock_execution(
            self.execution_manager,
            job_id,
            trace_id,
            agent_definition,
        )


class ExecutionFactory:
    """Factory for creating execution strategies."""

    @staticmethod
    def create_strategy(execution_manager: Any = None) -> ExecutionStrategy:
        if execution_manager is None:
            raise ValueError("Execution manager is required for all execution strategies")
        if _is_mock_mode():
            return MockExecutionStrategy(execution_manager)
        else:
            return RealExecutionStrategy(execution_manager)
