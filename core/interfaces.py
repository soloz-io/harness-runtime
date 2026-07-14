"""
Interfaces for harness-runtime topologies.
"""

from typing import Any, Dict

from langchain_core.runnables import Runnable
from typing_extensions import Protocol


class TopologyBuilder(Protocol):
    def build(
        self,
        definition: Dict[str, Any],
        available_tools: Dict[str, Any],
        checkpointer: Any,
        *,
        workspace_id: str | None = None,
        session_id: str | None = None,
        db_pool: Any = None,
        backend: Any = None,
        skills: list[str] | None = None,
        composite_backend: Any = None,
    ) -> Runnable[Any, Any]:
        """Compile and return a runnable graph based on the specific topology strategy.

        Args:
            definition: The raw agent definition dictionary.
            available_tools: A mapping of tool names to loaded tool callables.
            checkpointer: The LangGraph checkpointer for persistence.
            workspace_id: The workspace/workflow ID for cross-session artifact access.
            session_id: The current session ID (excluded from artifact queries).
            db_pool: A sync PostgreSQL connection pool for artifact DB queries.
            backend: Pre-built ArtifactBackend instance (takes priority over workspace_id/session_id/db_pool).
            skills: List of skill paths for SkillsMiddleware discovery.
            composite_backend: Pre-built CompositeBackend for skills file access.

        Returns:
            A runnable graph.
        """
