"""ask_user questions are reported to Waypoint, as the sandbox, without blocking."""

import httpx

from core.waypoint_reports import questions as qr
from core.waypoint_reports import sender as wr


class _Inline:
    """Runs the background thread's target at once, so the test can see it."""

    def __init__(self, target, args, daemon):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _setup(monkeypatch, tmp_path, token="pod-token", url="http://sdk:8080/"):
    token_file = tmp_path / "token"
    if token is not None:
        token_file.write_text(token)
    monkeypatch.setattr(wr, "IDENTITY_TOKEN_PATH", token_file)
    if url is None:
        monkeypatch.delenv("WAYPOINT_SDK_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("WAYPOINT_SDK_BASE_URL", url)
    monkeypatch.setattr(wr.threading, "Thread", _Inline)
    calls = []
    monkeypatch.setattr(
        wr.httpx,
        "post",
        lambda url, headers, timeout: calls.append((url, headers)) or httpx.Response(202),
    )
    return calls


ASK = {"type": "ai", "tool_calls": [{"name": "ask_user", "id": "call-1", "args": {}}]}
ANSWER = {"type": "tool", "tool_call_id": "call-1", "content": "Approved"}


def test_an_ask_user_call_with_no_answer_is_a_pending_question():
    assert qr.has_unanswered_question([{"type": "human"}, ASK])


def test_an_answered_question_in_the_history_is_not_reported_again():
    # A turn's first state carries the whole history: this must not re-raise it.
    assert not qr.has_unanswered_question(
        [{"type": "human"}, ASK, ANSWER, {"type": "ai", "content": "done"}]
    )


def test_a_new_question_after_an_answered_one_is_pending():
    later = {"type": "ai", "tool_calls": [{"name": "ask_user", "id": "call-2", "args": {}}]}
    assert qr.has_unanswered_question([ASK, ANSWER, later])


def test_other_tool_calls_and_empty_state_are_not_questions():
    assert not qr.has_unanswered_question(
        [{"type": "ai", "tool_calls": [{"name": "write_file", "id": "c"}]}]
    )
    assert not qr.has_unanswered_question([])


def test_reports_as_the_sandbox_and_names_no_session(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    qr.report_question("chat_video")
    assert calls == [("http://sdk:8080/internal/chat/questions", {wr.IDENTITY_HEADER: "pod-token"})]


def test_sends_nothing_without_an_sdk_address(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, url=None)
    qr.report_question("chat_video")
    assert calls == []


def test_sends_nothing_without_an_identity_token(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, token=None)
    qr.report_question("chat_video")
    assert calls == []


def test_a_failed_report_never_raises(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(wr.httpx, "post", boom)
    qr.report_question("chat_video")
