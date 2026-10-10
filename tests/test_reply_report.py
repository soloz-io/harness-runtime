"""The agent's replies are reported to Waypoint at turn end (ADR-025, amendment 2026-10-09)."""

import httpx

from core.waypoint_reports import replies as rr
from core.waypoint_reports import sender as wr


class _Inline:
    def __init__(self, target, args, daemon):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def test_a_main_agent_message_with_text_is_a_reply():
    assert rr.is_reply({"type": "ai", "content": "Here is the script."})
    assert rr.is_reply({"type": "ai", "content": [{"type": "text", "text": "Done."}]})


def test_tool_only_messages_tool_results_and_others_are_not_replies():
    assert not rr.is_reply(
        {"type": "ai", "content": "", "tool_calls": [{"name": "task", "id": "c"}]}
    )
    assert not rr.is_reply({"type": "ai", "content": [{"type": "tool_use", "id": "c"}]})
    assert not rr.is_reply({"type": "ai", "content": "   "})
    assert not rr.is_reply({"type": "tool", "content": "ok"})
    assert not rr.is_reply({"type": "human", "content": "hi"})
    assert not rr.is_reply({"type": "system", "content": "notice"})


def test_reports_as_the_sandbox(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("pod-token")
    monkeypatch.setattr(wr, "IDENTITY_TOKEN_PATH", token_file)
    monkeypatch.setenv("WAYPOINT_SDK_BASE_URL", "http://sdk:8080/")
    monkeypatch.setattr(wr.threading, "Thread", _Inline)
    calls = []
    monkeypatch.setattr(
        wr.httpx,
        "post",
        lambda url, headers, timeout: calls.append((url, headers)) or httpx.Response(202),
    )
    rr.report_reply("chat_video")
    assert calls == [("http://sdk:8080/internal/chat/replies", {wr.IDENTITY_HEADER: "pod-token"})]
