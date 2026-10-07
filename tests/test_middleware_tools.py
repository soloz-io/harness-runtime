"""Which agent gets which interaction tool.

Only the agent the user talks to may ask them anything. A specialist given
`ask_user` asks a question whose answer goes to the orchestrator, which never
saw it — and the options the specialist offered are lost with its context.
No agent has a `task_queue` tool: the harness starts the task loader from the
tool result itself (core/execution/queued_jobs.py).
"""

from __future__ import annotations

from core.graph.topology._shared import build_middleware_stack
from core.middleware.human_interaction import HumanInteractionMiddleware


def tool_names(middleware) -> set[str]:
    return {t.name for m in middleware for t in getattr(m, "tools", [])}


def test_human_interaction_is_ask_user_alone():
    assert tool_names([HumanInteractionMiddleware()]) == {"ask_user"}


def test_a_specialist_never_asks_the_user():
    tools = tool_names(build_middleware_stack({}, model=None))
    assert "ask_user" not in tools
    assert "task_queue" not in tools
