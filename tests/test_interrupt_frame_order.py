"""A turn's result reaches every client watching it, not only the sender.

A client reading the stream live stops at the turn's terminal lifecycle
event, so the result must come first, for every way a turn ends.
"""

import time

import pytest

from core.execution.handlers.root_values_handler import RootValuesHandler
from core.execution.state import ExecutionState
from core.publishers.event_publisher import StdioPublisher


class _Recorder(StdioPublisher):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def publish_assistant(self, **_):
        self.calls.append("assistant")

    def publish_result(self, **kwargs):
        self.calls.append(f"result:{kwargs.get('subtype')}")

    def publish_lifecycle_completed(self, **_):
        self.calls.append("lifecycle:completed")

    def publish_lifecycle_failed(self, **_):
        self.calls.append("lifecycle:failed")

    def publish_lifecycle_cancelled(self, **_):
        self.calls.append("lifecycle:cancelled")


@pytest.mark.parametrize(
    ("outcome", "subtype"),
    [("completed", "success"), ("failed", "error_during_execution"), ("cancelled", "cancelled")],
)
def test_a_turn_ends_with_its_result_then_its_lifecycle_event(outcome, subtype):
    p = _Recorder()
    p.publish_turn_end(session_id="s1", outcome=outcome, error="x", subtype=subtype)
    assert p.calls == [f"result:{subtype}", f"lifecycle:{outcome}"]


def test_a_question_is_on_the_stream_before_the_turn_completes():
    p = _Recorder()
    RootValuesHandler._publish_interrupt(
        [{"value": {"questions": [{"question": "Which tone?"}]}}],
        ExecutionState(),
        p,
        "s1",
        time.time(),
        1,
    )
    assert p.calls[-2:] == ["result:interrupted", "lifecycle:completed"]
