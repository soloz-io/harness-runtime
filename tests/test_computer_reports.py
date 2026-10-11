"""A session's computer reports to its agent the way a finished job does.

A computer/report notice is written to the chat, like every notice -- and a
notice is the chat's, not the graph's, so on its own the agent never read it:
the computer's report of a scene that threw showed in the chat and nothing was
fixed. The notice row is now the report's record: the main agent reads the
notice rows after the last one it saw before each model call, and an idle
agent is woken for a report it has not read.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

import core.middleware.computer_reports as reports
from core.execution.executor import ExecutionManager


def _notice(
    type_: str,
    message: str = "Scene 018 throws at motion.tsx line 112",
    title: str = "Scene 018 failed",
) -> str:
    return json.dumps({"type": type_, "title": title, "message": message})


class _Pool:
    """chat_messages rows (sequence, source, content) for one session."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    def connection(self):
        pool = self

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                pool.queries += 1
                _, seen = params
                self.result = [
                    (seq, content)
                    for seq, source, content in pool.rows
                    if source == "notification" and seq > seen
                ]

            def fetchall(self):
                return self.result

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return _Cursor()

        return _Conn()


ROWS = [
    (3, "deepagents", "hello"),
    (4, "notification", _notice("job/started")),
    (7, "notification", _notice("computer/report")),
    (9, "notification", _notice("audio/wav")),
]


def test_only_reports_are_delivered_and_every_notice_read_moves_the_mark():
    found, last = reports.unread_reports(_Pool(ROWS), "s1", -1)
    assert found == [(7, "Scene 018 throws at motion.tsx line 112")]
    assert last == 9
    update = reports.ComputerReportMiddleware._deliver(found, last)
    assert update[reports.SEEN_KEY] == 9
    (message,) = update["messages"]
    assert message.content == "[System Notification] Scene 018 throws at motion.tsx line 112"
    assert reports.is_computer_report({"additional_kwargs": message.additional_kwargs})


def test_a_report_already_read_is_not_delivered_again():
    found, last = reports.unread_reports(_Pool(ROWS), "s1", 7)
    assert found == [] and last == 9
    assert reports.ComputerReportMiddleware._deliver(found, last) == {reports.SEEN_KEY: 9}
    assert reports.ComputerReportMiddleware._deliver([], None) is None


def test_a_notice_that_is_not_a_report_or_has_no_message_is_not_one():
    assert reports.report_message_text(_notice("computer/report", message="  ")) is None
    assert reports.report_message_text(_notice("job/failed")) is None
    assert reports.report_message_text("Plain text notice") is None
    assert reports.report_message_text({"type": "computer/report"}) is None


def test_the_middleware_reads_the_running_sessions_rows(monkeypatch):
    pool = _Pool(ROWS)
    monkeypatch.setattr(reports, "_pool", lambda: pool)
    monkeypatch.setattr(reports, "get_config", lambda: {"configurable": {"thread_id": "s1"}})
    update = asyncio.run(reports.ComputerReportMiddleware().abefore_model({"messages": []}, None))
    assert [m.id for m in update["messages"]] == ["computer-report-7"]
    update = reports.ComputerReportMiddleware().before_model(
        {"messages": [], reports.SEEN_KEY: 9}, None
    )
    assert update is None


# ── The wake check ─────────────────────────────────────────────────────────


class State(TypedDict, total=False):
    computer_reports_seen: int
    answer: str


def _idle(saver: InMemorySaver, seen: int) -> None:
    g = StateGraph(State)
    g.add_node("run", lambda s: {"computer_reports_seen": seen})
    g.add_edge(START, "run")
    g.add_edge("run", END)
    g.compile(checkpointer=saver).invoke({}, {"configurable": {"thread_id": "s1"}})


def _paused(saver: InMemorySaver) -> None:
    g = StateGraph(State)
    g.add_node("ask", lambda s: {"answer": interrupt({"question": "Pick a style"})})
    g.add_edge(START, "ask")
    g.add_edge("ask", END)
    g.compile(checkpointer=saver).invoke({}, {"configurable": {"thread_id": "s1"}})


def _wake(saver: InMemorySaver, pool: _Pool) -> bool:
    manager = SimpleNamespace(_async_checkpointer=None, checkpointer=saver, _pool=pool)
    return asyncio.run(ExecutionManager.has_something_to_report(manager, "s1"))  # type: ignore[arg-type]


def test_an_unread_report_wakes_an_idle_agent():
    saver = InMemorySaver()
    _idle(saver, seen=4)
    assert _wake(saver, _Pool(ROWS))


def test_a_read_report_does_not_wake_it():
    saver = InMemorySaver()
    _idle(saver, seen=7)
    assert not _wake(saver, _Pool(ROWS))


def test_a_report_never_wakes_an_agent_with_a_question_open():
    saver = InMemorySaver()
    _paused(saver)
    assert not _wake(saver, _Pool(ROWS))


def test_a_chat_with_no_reports_does_not_wake():
    saver = InMemorySaver()
    _idle(saver, seen=-1)
    assert not _wake(saver, _Pool([r for r in ROWS if "computer/report" not in str(r[2])]))
