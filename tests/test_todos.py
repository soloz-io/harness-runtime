"""The simple todo list (core/middleware/todos.py): write_todos and a todos state, nothing more."""

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from core.middleware.todos import DESCRIPTION, TodoMiddleware


SEEN_TOOLS: list[str] = []


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):  # the fake has no tool binding; the agent needs one
        SEEN_TOOLS[:] = [t.name for t in tools]
        return self


def call(call_id, todos):
    return AIMessage(
        content="", tool_calls=[{"id": call_id, "name": "write_todos", "args": {"todos": todos}}]
    )


def test_the_agent_writes_and_replaces_its_list():
    first = [
        {"content": "Research the topic", "status": "in_progress"},
        {"content": "Write the brief", "status": "pending"},
    ]
    then = [
        {"content": "Research the topic", "status": "completed"},
        {"content": "Write the brief", "status": "in_progress"},
    ]
    model = FakeModel(
        messages=iter([call("t1", first), call("t2", then), AIMessage(content="Done.")])
    )
    result = create_agent(model=model, tools=[], middleware=[TodoMiddleware()]).invoke(
        {"messages": [("user", "go")]}
    )
    assert result["todos"] == then, "each call replaces the whole list"
    replies = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [r.content for r in replies] == ["Todo list updated.", "Todo list updated."], (
        "one line, not the list echoed"
    )
    assert "write_todos" in SEEN_TOOLS


def test_the_model_sees_only_the_list_and_a_short_description():
    tool = TodoMiddleware().tools[0]
    schema = tool.tool_call_schema.model_json_schema()
    assert list(schema["properties"]) == ["todos"], (
        "tool_call_id is injected, never the model's to fill"
    )
    assert tool.description == DESCRIPTION and len(DESCRIPTION) < 400
    assert "NOT use" not in DESCRIPTION and "trivial" not in DESCRIPTION


def test_it_adds_nothing_to_the_prompt():
    middleware = TodoMiddleware()
    assert type(middleware).wrap_model_call is TodoMiddleware.__mro__[1].wrap_model_call


@pytest.mark.parametrize(
    "bad", [[{"content": "x", "status": "done"}], [{"content": "", "status": "pending"}]]
)
def test_an_unknown_status_or_empty_item_is_refused(bad):
    with pytest.raises(ValidationError):
        TodoMiddleware().tools[0].args_schema(todos=bad, tool_call_id="t")
