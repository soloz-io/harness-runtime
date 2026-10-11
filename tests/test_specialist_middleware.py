"""Every specialist and subagent keeps a todo list (write_todos, core/middleware/todos.py)
and retries an unparseable tool call. Only the orchestrator asks the user: no
specialist gets ask_user.
"""

from core.graph.topology.specialist_builder import build_specialist_middleware
from core.graph.topology.subagent_builder import build_subagent
from core.middleware.human_interaction import HumanInteractionMiddleware
from core.middleware.invalid_tool_calls import InvalidToolCallRetryMiddleware
from core.middleware.todos import TodoMiddleware


def kinds(stack):
    return {type(m) for m in stack}


def test_a_specialist_plans_and_retries_but_cannot_ask_the_user():
    stack = kinds(build_specialist_middleware({}, model=None))
    assert InvalidToolCallRetryMiddleware in stack
    assert TodoMiddleware in stack
    assert HumanInteractionMiddleware not in stack


def test_a_nested_subagent_plans_and_retries_but_cannot_ask_the_user(monkeypatch):
    # Building the spec creates its model client; no call is made.
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test")
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "http://gateway.test")
    spec = build_subagent(
        {"name": "motion-graphics", "model": {"model_name": "deepseek-flash"}, "tools": []}, {}
    )
    stack = kinds(spec["middleware"])
    assert InvalidToolCallRetryMiddleware in stack
    assert TodoMiddleware in stack
    assert HumanInteractionMiddleware not in stack
