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
        # The list is left open, so the end-of-run check sends it back once.
        messages=iter(
            [
                call("t1", first),
                call("t2", then),
                AIMessage(content="Done."),
                AIMessage(content="Done."),
            ]
        )
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


# ---- The end-of-run check: a list left open sends the agent back once ----

from langchain_core.messages import HumanMessage  # noqa: E402

from core.middleware.todos import is_todo_reminder  # noqa: E402


def run(replies, user="go"):
    model = FakeModel(messages=iter(replies))
    return create_agent(model=model, tools=[], middleware=[TodoMiddleware()]).invoke(
        {"messages": [HumanMessage(user)]}
    )


def reminders(result):
    return [m for m in result["messages"] if is_todo_reminder(m)]


def test_a_list_left_open_sends_the_agent_back_to_close_it():
    open_list = [
        {"content": "Research the topic", "status": "completed"},
        {"content": "Write the brief", "status": "in_progress"},
    ]
    closed = [
        {"content": "Research the topic", "status": "completed"},
        {"content": "Write the brief", "status": "completed"},
    ]
    result = run(
        [
            call("t1", open_list),
            AIMessage(content="Done."),
            call("t2", closed),
            AIMessage(content="Done."),
        ]
    )
    assert len(reminders(result)) == 1
    notice = reminders(result)[0].content
    assert "[in_progress] Write the brief" in notice and "Research the topic" not in notice, (
        "it names only the open items"
    )
    assert result["todos"] == closed
    assert result["messages"][-1].content == "Done."


def test_a_closed_list_or_no_list_finishes_at_once():
    closed = [{"content": "Write the brief", "status": "completed"}]
    assert reminders(run([call("t1", closed), AIMessage(content="Done.")])) == []
    assert reminders(run([AIMessage(content="Done.")])) == []


def test_the_notice_is_given_once_per_run_then_the_answer_goes_through():
    open_list = [{"content": "Write the brief", "status": "pending"}]
    result = run(
        [call("t1", open_list), AIMessage(content="Done."), AIMessage(content="Still done.")]
    )
    assert len(reminders(result)) == 1, "never a second notice in one run: it cannot loop"
    assert result["messages"][-1].content == "Still done."
    assert result["todos"] == open_list, "the list stays as the agent wrote it"


def test_a_new_user_message_starts_a_new_run():
    open_list = [{"content": "Write the brief", "status": "pending"}]
    middleware = TodoMiddleware()
    earlier = [
        HumanMessage("first"),
        HumanMessage("[System Notification] …", additional_kwargs={"todo_reminder": True}),
        AIMessage(content="ok"),
        HumanMessage("second", additional_kwargs={"attachments": ["x"]}),
        AIMessage(content="Done."),
    ]
    update = middleware.after_model({"messages": earlier, "todos": open_list}, None)
    assert update and update["jump_to"] == "model", "the earlier turn's notice does not count"
    job_outcome = HumanMessage(
        "[System Notification] job done", additional_kwargs={"job_outcome": {"run_id": "r"}}
    )
    same_run = earlier + [update["messages"][0], job_outcome, AIMessage(content="Done.")]
    assert middleware.after_model({"messages": same_run, "todos": open_list}, None) is None, (
        "a job outcome is not a new run"
    )


def test_a_reply_still_working_is_left_alone():
    open_list = [{"content": "Write the brief", "status": "pending"}]
    working = AIMessage(content="", tool_calls=[{"id": "x", "name": "read_file", "args": {}}])
    assert (
        TodoMiddleware().after_model(
            {"messages": [HumanMessage("go"), working], "todos": open_list}, None
        )
        is None
    )


def test_the_notice_is_never_a_chat_row():
    from core.execution.helpers import serialize_messages_for_values

    notice = TodoMiddleware().after_model(
        {
            "messages": [HumanMessage("go"), AIMessage(content="Done.")],
            "todos": [{"content": "x", "status": "pending"}],
        },
        None,
    )["messages"][0]
    assert is_todo_reminder(serialize_messages_for_values([notice])[0]), (
        "the marker survives serialization, where both values handlers drop it"
    )
