"""Event handler chain — extensible dispatch for v3 protocol events.

Each handler has a single responsibility (SRP). Adding a new event type
means adding a new handler class — never editing existing code (OCP).
"""

from abc import ABC, abstractmethod
from typing import Any, Optional

from core.event_publisher import EventPublisher
from core.execution_state import ExecutionState
from core.tool_handlers import ToolHandlerRegistry
from core.types import Event


class EventHandler(ABC):
    """Single-responsibility handler for one class of v3 protocol events."""

    @abstractmethod
    def can_handle(self, event: Event) -> bool:
        """Return True if this handler should process *event*."""

    @abstractmethod
    def handle(
        self,
        event: Event,
        state: ExecutionState,
        publisher: EventPublisher,
        session_id: str,
        model_name: str,
        start_time: float,
        num_turns: int,
    ) -> bool | None:
        """Process *event* and mutate *state* / publish via *publisher*.

        Return ``False`` to signal the execution loop should stop
        (e.g. on interrupt); ``None`` or ``True`` to continue.
        """


def create_handler_chain(
    pool: Optional[Any] = None,
    tool_handler_registry: Optional[ToolHandlerRegistry] = None,
) -> list[EventHandler]:
    """Build the default handler chain in dispatch order.

    Parameters
    ----------
    pool : psycopg pool or None
        Database connection pool for ``RootValuesHandler``.
    tool_handler_registry : ToolHandlerRegistry or None
        Registry for per-tool event handlers. If not provided, a default
        registry (with ``DefaultToolHandler``) is created internally.
    """
    from core.handlers.lifecycle_handler import LifecycleHandler
    from core.handlers.root_message_handler import RootMessageHandler
    from core.handlers.root_tools_handler import RootToolsHandler
    from core.handlers.root_values_handler import RootValuesHandler
    from core.handlers.subagent_lifecycle_handler import SubagentLifecycleHandler
    from core.handlers.subagent_message_handler import SubagentMessageHandler
    from core.handlers.subagent_tools_handler import SubagentToolsHandler
    from core.handlers.subagent_values_handler import SubagentValuesHandler

    return [
        LifecycleHandler(),
        SubagentMessageHandler(),
        SubagentValuesHandler(),
        SubagentLifecycleHandler(),
        SubagentToolsHandler(),
        RootMessageHandler(),
        RootToolsHandler(registry=tool_handler_registry),
        RootValuesHandler(pool=pool),
    ]
