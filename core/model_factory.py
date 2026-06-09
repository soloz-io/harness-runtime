"""
Model factory for creating LLM instances based on configuration.

This factory abstracts the creation of LLM models, allowing for clean
separation between production and test implementations.
"""

import os
from typing import Any
from tests.utils.test_config import TestConfig


class ModelFactory:
    """Factory for creating LLM model instances."""
    
    @staticmethod
    def create_model() -> Any:
        """
        Create the appropriate LLM model based on configuration.
        
        Returns:
            LLM model instance (real or mock based on environment)
        """
        if TestConfig.is_mock_mode():
            return ModelFactory._create_mock_model()
        else:
            return ModelFactory._create_real_model()
    
    @staticmethod
    def _create_mock_model() -> Any:
        """Create a mock LLM model for testing."""
        from tests.utils.mock_workflow import get_mock_model_with_event_replay
        print("[MODEL_FACTORY] Creating mock LLM model")
        return get_mock_model_with_event_replay()
    
    @staticmethod
    def _create_real_model() -> Any:
        """Create a real LLM model for production."""
        from langchain_openai import ChatOpenAI
        model_name = os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")
        print(f"[MODEL_FACTORY] Creating real LLM model: {model_name}")
        return ChatOpenAI(model=model_name)
    
    @staticmethod
    def is_mock_mode() -> bool:
        """Check if we're in mock mode."""
        return TestConfig.is_mock_mode()


class ExecutionStrategy:
    """Base class for execution strategies."""
    
    def execute_workflow(self, graph_builder, agent_definition, job_id: str, trace_id: str) -> Any:
        """Execute the workflow with the appropriate strategy."""
        raise NotImplementedError


class RealExecutionStrategy(ExecutionStrategy):
    """Real execution strategy using actual LLM calls."""
    
    def __init__(self, execution_manager):
        self.execution_manager = execution_manager
    
    def execute_workflow(self, graph_builder, agent_definition, job_id: str, trace_id: str) -> Any:
        """Execute workflow with real LLM."""
        print(f"[EXECUTION] Real LLM execution for job_id: {job_id}")
        compiled_graph = graph_builder.build_from_definition(agent_definition)
        
        # Use the execution manager for proper execution
        input_payload = {"user_request": agent_definition.get("user_request", "")}
        result = self.execution_manager.execute(
            graph=compiled_graph,
            job_id=job_id,
            input_payload=input_payload,
            trace_id=trace_id
        )
        return result


class MockExecutionStrategy(ExecutionStrategy):
    """Mock execution strategy using event replay."""
    
    def __init__(self, execution_manager):
        self.execution_manager = execution_manager
    
    def execute_workflow(self, graph_builder, agent_definition, job_id: str, trace_id: str) -> Any:
        """Execute workflow with mock LLM and event replay."""
        from tests.utils.mock_workflow import handle_mock_execution
        
        print(f"[EXECUTION] Mock LLM execution for job_id: {job_id}")
        
        # Build the graph with mock model first
        compiled_graph = graph_builder.build_from_definition(agent_definition)
        
        # Then handle mock execution
        return handle_mock_execution(
            self.execution_manager,
            job_id,
            trace_id,
            agent_definition
        )


class ExecutionFactory:
    """Factory for creating execution strategies."""
    
    @staticmethod
    def create_strategy(execution_manager=None) -> ExecutionStrategy:
        """
        Create the appropriate execution strategy.
        
        Args:
            execution_manager: Execution manager instance (required in mock mode)
            
        Returns:
            Execution strategy instance
        """
        if execution_manager is None:
            raise ValueError("Execution manager is required for all execution strategies")
        
        # Debug logging to track what's happening
        import os
        mock_env = os.getenv("USE_MOCK_LLM", "not_set")
        is_mock = TestConfig.is_mock_mode()
        print(f"[EXECUTION_FACTORY] USE_MOCK_LLM env var: '{mock_env}'")
        print(f"[EXECUTION_FACTORY] TestConfig.is_mock_mode(): {is_mock}")
        
        if is_mock:
            print(f"[EXECUTION_FACTORY] Creating MockExecutionStrategy")
            return MockExecutionStrategy(execution_manager)
        else:
            print(f"[EXECUTION_FACTORY] Creating RealExecutionStrategy")
            return RealExecutionStrategy(execution_manager)