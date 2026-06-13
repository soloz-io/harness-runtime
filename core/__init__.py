"""Core business logic for graph building and execution.

Inter-node communication is artifact-based (filesystem read_file/write_file),
not message-history-based. Message contexts are isolated per node in the
custom DAG -- see custom_graph_builder.py make_isolated_agent_node.
"""

# Main API
from core.builder import GraphBuilder, GraphBuilderError
from core.model_identifier import create_model_identifier
from core.subagent_builder import SubAgentCompilationError, build_subagent

# Modular functions
from core.tool_loader import ToolLoadingError, load_tools_from_definition

__all__ = [
    # Main API
    "GraphBuilder",
    "GraphBuilderError",

    # Modular functions
    "load_tools_from_definition",
    "create_model_identifier",
    "build_subagent",

    # Exceptions
    "ToolLoadingError",
    "SubAgentCompilationError"
]
