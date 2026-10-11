"""A turn the user stopped is recorded as stopped in the stored history.

Calls in flight when the run is cancelled never get a result, so a chat
rebuilt from history showed them still running. record_stopped_steps closes
each with an empty result row marked `stopped`, grouped where its call is.
"""

import core.persistence.message_writer as writer


class _Cursor:
    def __init__(self, rows, fail):
        self.rows, self.fail = rows, fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        if self.fail:
            raise RuntimeError("db down")

    def fetchall(self):
        return self.rows


class _Pool:
    def __init__(self, rows, fail=False):
        self.rows, self.fail = rows, fail

    def connection(self):
        pool = self

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return _Cursor(pool.rows, pool.fail)

        return _Conn()


TURN = [
    # The orchestrator dispatched media-generator, which never returned.
    ("deepagents", {"type": "ai", "tool_calls": [{"id": "task-1", "name": "task", "args": {}}]}),
    # Inside it: one call answered, one still running.
    (
        "subagent",
        {
            "type": "ai",
            "additional_kwargs": {"namespace": ["tools:abc"], "tool_call_id": "task-1"},
            "tool_calls": [{"id": "read-1", "name": "read_file"}, {"id": "grep-1", "name": "grep"}],
        },
    ),
    ("subagent", {"type": "tool", "tool_call_id": "read-1", "content": "ok"}),
]


def test_only_unanswered_calls_are_closed():
    found = [(src, call["id"]) for src, call, _ in writer._unanswered_calls(TURN)]
    assert found == [("deepagents", "task-1"), ("subagent", "grep-1")]


def test_each_unanswered_call_gets_one_empty_stopped_result_where_its_call_is(monkeypatch):
    written = []
    monkeypatch.setattr(
        writer,
        "write_chat_messages",
        lambda pool, sid, msgs, offset, source: written.append((source, msgs)),
    )
    assert writer.record_stopped_steps(_Pool(TURN), "s1") == 2
    by_source = dict(written)
    assert by_source["deepagents"] == [
        {
            "type": "tool",
            "id": "stopped-task-1",
            "tool_call_id": "task-1",
            "name": "task",
            "content": "",
            "additional_kwargs": {"stopped": True},
        }
    ]
    assert by_source["subagent"] == [
        {
            "type": "tool",
            "id": "stopped-grep-1",
            "tool_call_id": "grep-1",
            "name": "grep",
            "content": "",
            "additional_kwargs": {
                "stopped": True,
                "namespace": ["tools:abc"],
                "tool_call_id": "task-1",
            },
        }
    ]


def test_nothing_unanswered_writes_nothing_and_a_failed_read_is_not_raised(monkeypatch):
    written = []
    monkeypatch.setattr(writer, "write_chat_messages", lambda *a, **k: written.append(a))
    answered = [
        ("deepagents", {"type": "ai", "tool_calls": [{"id": "t", "name": "x"}]}),
        ("deepagents", {"type": "tool", "tool_call_id": "t"}),
    ]
    assert writer.record_stopped_steps(_Pool(answered), "s1") == 0
    assert writer.record_stopped_steps(_Pool([], fail=True), "s1") == 0
    assert written == []
