"""Unit tests for the non-blocking `task_queue` tool.

Covers the three properties the tool's contract rests on:

1. It returns immediately with a string — it never calls ``interrupt()``.
2. Payloads accumulate (several jobs queued in one superstep) and are drained
   exactly once.
3. Payloads are keyed by session (the graph's ``configurable.thread_id``), so
   two sessions sharing a process never read each other's jobs.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables.config import var_child_runnable_config

from core.execution_state import ExecutionState
from core.handlers.root_values_handler import RootValuesHandler
from core.middleware.human_interaction.task_queue import (
    UNSCOPED_KEY,
    _clear_all_payloads,
    consume_task_queue_payloads,
    task_queue,
)
from core.types import Event


class RecordingPublisher:
    """Duck-typed stand-in for EventPublisher that records task frames."""

    def __init__(self) -> None:
        self.task_frames: list[dict[str, Any]] = []

    def publish_task_queued(self, *, session_id: str, task: dict[str, Any]) -> None:
        self.task_frames.append({"session_id": session_id, "task": task})

    def publish_values(self, **kwargs: Any) -> None:
        pass

    def publish_checkpoint(self, **kwargs: Any) -> None:
        pass


def _record_in_session(thread_id: str, **kwargs: Any) -> str:
    """Invoke the tool with a runnable config whose thread_id is ``thread_id``."""
    token = var_child_runnable_config.set({"configurable": {"thread_id": thread_id}})
    try:
        return str(task_queue.invoke(kwargs))
    finally:
        var_child_runnable_config.reset(token)


def setup_function() -> None:
    _clear_all_payloads()


def teardown_function() -> None:
    _clear_all_payloads()


def test_task_queue_returns_immediately() -> None:
    """Must return a plain string acknowledgement — never interrupt()."""
    result = task_queue.invoke({"job_name": "Test Job", "run_id": "run_123"})
    assert isinstance(result, str)
    assert "run_123" in result


def test_task_queue_records_payload_and_drains_once() -> None:
    task_queue.invoke({"job_name": "Voice-over", "run_id": "abc"})

    payload = consume_task_queue_payloads(UNSCOPED_KEY)
    assert payload == [{"job_name": "Voice-over", "run_id": "abc", "description": None}]
    # Drained exactly once.
    assert consume_task_queue_payloads(UNSCOPED_KEY) == []


def test_task_queue_accumulates_multiple_jobs() -> None:
    """Two queue calls before the values event both survive (audio + video)."""
    task_queue.invoke({"job_name": "Voice-over", "run_id": "run_a"})
    task_queue.invoke({"job_name": "Avatar clips", "run_id": "run_b"})

    payloads = consume_task_queue_payloads(UNSCOPED_KEY)
    assert [p["run_id"] for p in payloads] == ["run_a", "run_b"]


def test_task_queue_job_id_is_run_id_alias() -> None:
    task_queue.invoke({"job_name": "Job", "job_id": "legacy_1"})
    assert consume_task_queue_payloads(UNSCOPED_KEY)[0]["run_id"] == "legacy_1"


def test_payloads_are_keyed_by_session() -> None:
    """A payload recorded under one session's thread_id is invisible to another."""
    _record_in_session("sess-a", job_name="A's job", run_id="a1")
    _record_in_session("sess-b", job_name="B's job", run_id="b1")

    drained_a = consume_task_queue_payloads("sess-a")
    assert [p["run_id"] for p in drained_a] == ["a1"]

    drained_b = consume_task_queue_payloads("sess-b")
    assert [p["run_id"] for p in drained_b] == ["b1"]

    assert consume_task_queue_payloads("sess-a") == []


def test_root_values_handler_emits_task_queued_frame() -> None:
    """The values handler drains the session's list and emits one frame per job."""
    _record_in_session("sess-1", job_name="Voice-over", run_id="r1")
    _record_in_session("sess-1", job_name="Avatar clips", run_id="r2")

    publisher = RecordingPublisher()
    state = ExecutionState()
    handler = RootValuesHandler(pool=None)
    event = Event(method="values", namespace=(), data={"messages": []})

    result = handler.handle(
        event,
        state,
        publisher,  # type: ignore[arg-type]
        session_id="sess-1",
        model_name="test-model",
        start_time=0.0,
        num_turns=1,
    )

    assert result is True  # non-blocking: the stream keeps going
    assert [f["session_id"] for f in publisher.task_frames] == ["sess-1", "sess-1"]
    assert [f["task"]["run_id"] for f in publisher.task_frames] == ["r1", "r2"]
    assert state.pending_task_queue == []

    # A second values event with nothing queued emits nothing new.
    handler.handle(
        event,
        state,
        publisher,  # type: ignore[arg-type]
        session_id="sess-1",
        model_name="test-model",
        start_time=0.0,
        num_turns=1,
    )
    assert len(publisher.task_frames) == 2


def test_root_values_handler_does_not_cross_sessions() -> None:
    """Session B's values event must not drain session A's queue."""
    _record_in_session("sess-a", job_name="A's job", run_id="a1")

    publisher = RecordingPublisher()
    handler = RootValuesHandler(pool=None)
    event = Event(method="values", namespace=(), data={"messages": []})

    handler.handle(
        event,
        ExecutionState(),
        publisher,  # type: ignore[arg-type]
        session_id="sess-b",
        model_name="test-model",
        start_time=0.0,
        num_turns=1,
    )
    assert publisher.task_frames == []

    handler.handle(
        event,
        ExecutionState(),
        publisher,  # type: ignore[arg-type]
        session_id="sess-a",
        model_name="test-model",
        start_time=0.0,
        num_turns=1,
    )
    assert [f["task"]["run_id"] for f in publisher.task_frames] == ["a1"]
