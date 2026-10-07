"""has_pending_question reads a paused graph from a real checkpointer.

It used to read `metadata["next"]` and an `interrupts` attribute -- neither of
which a CheckpointTuple has -- so it always answered "nothing pending": a
[System Notification] then started a fresh turn that cancelled the user's open
dialog. This runs a real graph to a real interrupt.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from core.execution.executor import ExecutionManager


class State(TypedDict, total=False):
    answer: str


def ask(state: State) -> State:
    return {"answer": interrupt({"question": "Pick a style"})}


def build(saver: InMemorySaver):
    g = StateGraph(State)
    g.add_node("ask", ask)
    g.add_edge(START, "ask")
    g.add_edge("ask", END)
    return g.compile(checkpointer=saver)


def pending(saver: InMemorySaver, thread: str) -> bool:
    manager = SimpleNamespace(_async_checkpointer=None, checkpointer=saver)
    return asyncio.run(ExecutionManager.has_pending_question(manager, thread))  # type: ignore[arg-type]


def test_a_graph_paused_on_a_question_has_one_pending():
    saver = InMemorySaver()
    graph = build(saver)
    config = {"configurable": {"thread_id": "t1"}}
    graph.invoke({}, config)
    assert pending(saver, "t1")


def test_once_answered_nothing_is_pending():
    saver = InMemorySaver()
    graph = build(saver)
    config = {"configurable": {"thread_id": "t1"}}
    graph.invoke({}, config)
    graph.invoke(Command(resume="Style 2"), config)
    assert not pending(saver, "t1")


def test_an_unknown_session_has_nothing_pending():
    assert not pending(InMemorySaver(), "nobody")


def _manager(saver: InMemorySaver) -> ExecutionManager:
    manager = ExecutionManager.__new__(ExecutionManager)
    manager._async_checkpointer = None  # type: ignore[attr-defined]
    manager.checkpointer = saver  # type: ignore[attr-defined]
    return manager


def test_an_answer_resumes_the_paused_graph_instead_of_starting_a_new_turn():
    # This read metadata["next"] -- absent on a CheckpointTuple -- so every answer
    # fell back to a fresh human turn and the question was abandoned.
    saver = InMemorySaver()
    graph = build(saver)
    config = {"configurable": {"thread_id": "t1"}}
    graph.invoke({}, config)

    manager = _manager(saver)
    decision = {"decisions": [{"type": "respond", "message": "Style 2"}]}
    stream_input = asyncio.run(
        manager._build_resume_input(
            {"messages": [{"role": "user", "content": "Style 2"}]}, decision, "t1"
        )
    )

    assert isinstance(stream_input, Command)
    result = graph.invoke(stream_input, config)
    # The interrupt receives the decision (ask_user unpacks its message).
    assert result["answer"] == decision
    assert not pending(saver, "t1")


def test_once_the_graph_has_finished_an_answer_is_an_ordinary_turn():
    saver = InMemorySaver()
    graph = build(saver)
    config = {"configurable": {"thread_id": "t1"}}
    graph.invoke({}, config)
    graph.invoke(Command(resume="Style 2"), config)

    payload = {"messages": [{"role": "user", "content": "hello"}]}
    decision = {"decisions": [{"type": "respond", "message": "hello"}]}
    assert asyncio.run(_manager(saver)._build_resume_input(payload, decision, "t1")) is payload
