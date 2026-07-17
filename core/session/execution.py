from typing import Any, Optional

from core.factory import build_agent_from_definition
from core.tool_registry import ToolRegistry


def prepare_turn_input(base_payload: dict[str, Any], user_content: str) -> dict[str, Any]:
    """Return a shallow copy of ``base_payload`` with ``user_content`` appended as a message.

    This is a pure function — no side effects.
    """
    payload = dict(base_payload)
    messages = list(payload.get("messages", []))
    if user_content:
        messages.append({"role": "user", "content": user_content})
    payload["messages"] = messages
    return payload


def consume_resume(session: Any) -> Optional[Any]:
    """Consume and clear any stored resume payload.

    Returns the resume payload or ``None``.
    """
    resume = getattr(session, "resume_payload", None)
    if resume is not None:
        session.resume_payload = None
    return resume


def build_graph(
    agent_definition: dict[str, Any],
    checkpointer: Any,
    tool_registry: ToolRegistry,
    workspace_id: str,
    session_id: str,
    backend: Any,
    composite_backend: Any,
) -> Any:
    """Build a compiled LangGraph from the agent definition.

    All session-level dependencies are passed explicitly so callers
    do not need to reach into ``Session`` internals.
    """
    return build_agent_from_definition(
        agent_definition,
        checkpointer=checkpointer,
        tool_registry=tool_registry,
        workspace_id=workspace_id,
        session_id=session_id,
        backend=backend,
        composite_backend=composite_backend,
    )


def initialize_tool_registry(agent_definition: dict[str, Any]) -> ToolRegistry:
    """Load tool definitions from the agent definition into a fresh ToolRegistry."""
    from core.embedded_tool_loader import ToolLoadingError, load_tool_implementations

    registry = ToolRegistry()
    tool_definitions = agent_definition.get("tool_definitions", [])
    if tool_definitions:
        try:
            load_tool_implementations(tool_definitions, registry)
        except ToolLoadingError as e:
            import structlog

            structlog.get_logger(__name__).error("tool_loading_failed", error=str(e))
            raise
    return registry
