"""The harness runtime: builds an agent graph from a definition and runs it.

Layout, one package per concern:

    graph/            building the agent graph from a definition (entry point:
                      graph.factory.build_agent_from_definition)
    llm/              creating the language model a node runs on
    tools/            loading and registering the tools a node is given
    middleware/       the agent's built-in tools and middleware stack
    execution/        running a turn and handling the events it streams
    publishers/       publishing those events to clients
    persistence/      what is written to the database for others to read
    workspace/        the filesystem an agent works on
    restore/          restoring a session to an earlier point
    session/          a session's lifecycle: pack, skills, configuration
    metro/            the Metro bundler integration for app previews
    waypoint_reports/ reports from the sandbox to Waypoint
    services.py       the dependency-injection container

Usage:
    from core import build_agent_from_definition

    agent = build_agent_from_definition(definition)

Inter-node communication is artifact-based (filesystem read_file/write_file),
not message-history-based. Message contexts are isolated per node in the
acrylic DAG.
"""

from core.graph.factory import build_agent_from_definition
from core.graph.topology.subagent_builder import SubAgentCompilationError, build_subagent
from core.llm.identifier import create_model_identifier

# Modular functions
from core.tools.loader import ToolLoadingError, load_tools_from_definition

__all__ = [
    # Main API
    "build_agent_from_definition",
    # Modular functions
    "load_tools_from_definition",
    "create_model_identifier",
    "build_subagent",
    # Exceptions
    "ToolLoadingError",
    "SubAgentCompilationError",
]
