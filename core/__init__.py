"""Core business logic for graph building and execution.

This module provides the main entry point for the agent executor service.
The implementation follows a flat, modular structure:

Flat Structure:
    - factory.py: Main factory function (entry point)
    - model_identifier.py: Model identifier creation
    - subagent_builder.py: Subagent compilation logic
    - tool_loader.py: Tool loading logic

Usage:
    from core import build_agent_from_definition

    agent = build_agent_from_definition(definition)

Or use the modular functions directly:
    from core import (
        load_tools_from_definition,
        create_model_identifier,
        build_subagent
    )

Inter-node communication is artifact-based (filesystem read_file/write_file),
not message-history-based. Message contexts are isolated per node in the
acrylic DAG.
"""

from core.factory import build_agent_from_definition
from core.model_identifier import create_model_identifier
from core.subagent_builder import SubAgentCompilationError, build_subagent

# Modular functions
from core.tool_loader import ToolLoadingError, load_tools_from_definition

__all__ = [
    # Main API
    "build_agent_from_definition",

    # Modular functions
    "load_tools_from_definition",
    "create_model_identifier",
    "build_subagent",

    # Exceptions
    "ToolLoadingError",
    "SubAgentCompilationError"
]
