"""A workspace change is reported to Waypoint when a writing tool finishes, throttled."""

import pytest

from core.waypoint_reports import workspace as ws


@pytest.fixture
def sent(monkeypatch):
    """The reports sent, and a clock the test moves."""

    class _Sent(list):
        clock: dict

    calls = _Sent()
    clock = {"now": 100.0}
    monkeypatch.setattr(
        ws, "post_as_sandbox", lambda route, session_id: calls.append((route, session_id))
    )
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock["now"])
    ws._state.clear()
    calls.clock = clock
    return calls


def test_a_tool_that_can_write_reports_the_change(sent):
    ws.note_tool_finished("chat_a", "write_file")
    assert sent == [("/internal/chat/files-changed", "chat_a")]


@pytest.mark.parametrize("tool", ["edit_file", "execute", "run_tool", "eval"])
def test_every_writing_tool_reports(sent, tool):
    ws.note_tool_finished("chat_a", tool)
    assert len(sent) == 1


@pytest.mark.parametrize(
    "tool", ["read_file", "ls", "grep", "task", "ask_user", "task_queue", "unknown"]
)
def test_a_tool_that_cannot_write_reports_nothing(sent, tool):
    ws.note_tool_finished("chat_a", tool)
    ws.flush_workspace_change("chat_a")
    assert sent == []


def test_a_burst_is_one_report_now_and_one_at_the_end_of_the_turn(sent):
    for _ in range(3):
        ws.note_tool_finished("chat_a", "write_file")
    assert len(sent) == 1
    ws.flush_workspace_change("chat_a")
    assert len(sent) == 2
    # Nothing is left to report after it.
    ws.flush_workspace_change("chat_a")
    assert len(sent) == 2


def test_a_change_after_the_window_is_reported_at_once(sent):
    ws.note_tool_finished("chat_a", "write_file")
    sent.clock["now"] += ws.MIN_INTERVAL_SECONDS + 0.1
    ws.note_tool_finished("chat_a", "edit_file")
    assert len(sent) == 2


def test_a_flush_with_nothing_held_back_sends_nothing(sent):
    ws.note_tool_finished("chat_a", "write_file")
    ws.flush_workspace_change("chat_a")
    assert len(sent) == 1
    ws.flush_workspace_change("chat_never_wrote")
    assert len(sent) == 1


def test_sessions_are_throttled_apart(sent):
    ws.note_tool_finished("chat_a", "write_file")
    ws.note_tool_finished("chat_b", "write_file")
    assert sent == [
        ("/internal/chat/files-changed", "chat_a"),
        ("/internal/chat/files-changed", "chat_b"),
    ]


def test_new_tool_messages_from_a_writing_tool_report_once(sent):
    ws.note_new_messages(
        "chat_a",
        [
            {"type": "ai", "tool_calls": [{"name": "write_file"}]},
            {"type": "tool", "name": "write_file", "content": "Updated file /workspace/a.md"},
            {"type": "tool", "name": "edit_file", "content": "ok"},
        ],
    )
    assert sent == [("/internal/chat/files-changed", "chat_a")]


def test_new_messages_without_a_writing_tool_report_nothing(sent):
    ws.note_new_messages(
        "chat_a", [{"type": "tool", "name": "read_file"}, {"type": "ai", "content": "done"}]
    )
    assert sent == []


def test_a_specialists_write_reports_against_the_root_session(sent, monkeypatch):
    """Observed: a specialist's writes arrive as tool messages in its state
    updates, not as tool events -- the handler for its values must report them."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    import core.execution.handlers.subagent_values_handler as handler_mod
    from core.execution.state import ExecutionState
    from core.execution.types import Event

    monkeypatch.setattr(handler_mod, "note_new_messages", ws.note_new_messages)

    class _Publisher:
        def publish_values(self, **_kw):
            pass

    msgs = [
        HumanMessage(content="Objective: record the brand brief"),
        AIMessage(content="", tool_calls=[{"name": "write_file", "args": {}, "id": "c1"}]),
        ToolMessage(
            content="Updated file /workspace/.global/x.md", name="write_file", tool_call_id="c1"
        ),
    ]
    event = Event(method="values", namespace=("brand-discovery:abc",), data={"messages": msgs})
    handler_mod.SubagentValuesHandler(pool=None).handle(
        event, ExecutionState(), _Publisher(), "chat_root", "m", 0.0, 1
    )
    assert sent == [("/internal/chat/files-changed", "chat_root")]
