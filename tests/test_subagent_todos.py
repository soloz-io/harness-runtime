"""Each agent's todo list reaches the browser live, as the agent wrote it (waypoint ADR-052)."""

from core.execution.handlers.subagent_values_handler import SubagentValuesHandler
from core.execution.state import ExecutionState
from core.execution.types import Event


class Publisher:
    def __init__(self):
        self.values = []

    def publish_values(self, **kwargs):
        self.values.append(kwargs)


TODOS = [
    {"content": "Research the topic", "status": "completed"},
    {"content": "Write the creator brief", "status": "in_progress"},
]


def handle(handler, state, publisher, ns, data):
    handler.handle(Event("values", ns, data), state, publisher, "chat_a", "m", 0.0, 1)


def test_an_agents_todos_are_published_as_written():
    state, publisher = ExecutionState(), Publisher()
    ns = ("content-discovery:abc",)
    state.subagent_names[ns] = "content-discovery"
    handle(SubagentValuesHandler(pool=None), state, publisher, ns, {"todos": TODOS})
    assert publisher.values == [
        {
            "session_id": "chat_a",
            "messages": [],
            "todos": {
                "agent": "content-discovery",
                "namespace": ["content-discovery:abc"],
                "todos": TODOS,
            },
        }
    ]


def test_an_unchanged_list_is_not_sent_again_and_a_changed_one_is():
    state, publisher = ExecutionState(), Publisher()
    ns = ("media-generator:1", "tools:2")
    handler = SubagentValuesHandler(pool=None)
    handle(handler, state, publisher, ns, {"todos": TODOS})
    handle(handler, state, publisher, ns, {"todos": TODOS})
    assert len(publisher.values) == 1
    done = [dict(t, status="completed") for t in TODOS]
    handle(handler, state, publisher, ns, {"todos": done})
    assert publisher.values[-1]["todos"]["todos"] == done
    # No name recorded for this namespace: its last segment names the agent.
    assert publisher.values[-1]["todos"]["agent"] == "tools"


def test_no_todos_publishes_nothing():
    state, publisher = ExecutionState(), Publisher()
    handle(SubagentValuesHandler(pool=None), state, publisher, ("a:1",), {"messages": []})
    assert publisher.values == []
