"""
Agent Executor Graph Factory.

Factory function for creating agent executor graphs from definition.json files.
Used by LangGraph CLI for development and testing.
"""

import json
from pathlib import Path
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver

from core.builder import GraphBuilder


def load_definition(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Definition not found at {path}")
    with open(path) as f:
        return json.load(f)


def create_deepagents_runtime(checkpointer: Optional[BaseCheckpointSaver] = None):
    definition_path = Path(__file__).parent / "tests" / "mock" / "definition.json"
    definition = load_definition(definition_path)
    builder = GraphBuilder(checkpointer=checkpointer)
    return builder.build_from_definition(definition)
