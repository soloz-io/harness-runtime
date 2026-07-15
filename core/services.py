"""Dependency injection container for runtime services.

Provides a single point of composition for all runtime dependencies,
eliminating global module-level state.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from core.event_publisher import EventPublisher
from core.executor import ExecutionManager
from core.tool_registry import ToolRegistry


@dataclass
class RuntimeServices:
    """Container for all shared runtime services.

    Bootstrap once at application startup (in ``cli.py`` or
    ``api/main.py``), then pass through the call chain.
    """

    publisher: EventPublisher
    execution_manager: ExecutionManager
    db_pool: Optional[Any] = None

    # Session store (mutable — maintained in-memory by the API layer)
    session_store: dict[str, Any] = field(default_factory=dict)

    # Tool registry shared across sessions
    tool_registry: ToolRegistry = field(default_factory=ToolRegistry)

    # Redis client for SSE event streaming
    redis_client: Optional[Any] = None


_SERVICES: Optional[RuntimeServices] = None


def init_services(services: RuntimeServices) -> None:
    """Set the global services singleton (bootstrap at startup)."""
    global _SERVICES
    _SERVICES = services


def get_services() -> RuntimeServices:
    """Retrieve the global services singleton."""
    assert _SERVICES is not None, "RuntimeServices not initialized"
    return _SERVICES
