"""
Interfaces for harness-runtime topologies.
"""

from typing import Any, Dict
from typing_extensions import Protocol
from langchain_core.runnables import Runnable

class TopologyBuilder(Protocol):
    def build(
        self,
        definition: Dict[str, Any],
        available_tools: Dict[str, Any],
        checkpointer: Any,
    ) -> Runnable[Any, Any]:
        """Compile and return a runnable graph based on the specific topology strategy.
        
        Args:
            definition: The raw agent definition dictionary.
            available_tools: A mapping of tool names to loaded tool callables.
            checkpointer: The LangGraph checkpointer for persistence.
            
        Returns:
            A runnable graph.
        """
