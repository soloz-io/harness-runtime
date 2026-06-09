"""
Agent Executor Graph Factory.

This module provides the factory function for creating agent executor graphs
from definition.json files. It's used by LangGraph CLI for development and testing.

Similar to spec-engine/agent.py, this creates a graph from a test definition
for development purposes.
"""

import sys
from pathlib import Path
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver

from core.builder import GraphBuilder

# Add tests directory to path to import test helpers
tests_dir = Path(__file__).parent / "tests"
sys.path.insert(0, str(tests_dir))

from integration.test_helpers import load_definition_with_files


def create_deepagents_runtime(checkpointer: Optional[BaseCheckpointSaver] = None):
    """
    Creates a deepagents runtime graph from the test definition.
    
    This function loads the test definition.json and builds a graph for
    development and testing purposes. LangGraph CLI automatically provides
    a PostgreSQL checkpointer when POSTGRES_URI is set in the environment.
    
    Args:
        checkpointer: Optional checkpointer instance. LangGraph CLI automatically
                     provides a PostgreSQL checkpointer when POSTGRES_URI is set.
        
    Returns:
        Compiled agent graph ready for execution
    """
    print("🚀 create_deepagents_runtime called!")
    print(f"📦 Checkpointer provided by LangGraph CLI: {checkpointer is not None}")
    print(f"🔧 Checkpointer type: {type(checkpointer) if checkpointer else 'None'}")
    
    # Load test definition with prompts and tools from files
    definition_path = Path(__file__).parent / "tests" / "mock" / "definition.json"
    
    if not definition_path.exists():
        raise FileNotFoundError(
            f"Test definition not found at {definition_path}. "
            "Please ensure tests/mock/definition.json exists."
        )
    
    # Use helper to load definition with prompts and tools from files
    definition = load_definition_with_files(definition_path)
    
    print(f"✓ Loaded definition with {len(definition.get('nodes', []))} nodes")
    
    # Build graph using GraphBuilder
    builder = GraphBuilder(checkpointer=checkpointer)
    agent = builder.build_from_definition(definition)
    
    print("✓ Agent graph built successfully")
    return agent
