"""
Question report — tells Waypoint that this session's agent asked the user a
question (waypoint ADR-025, amendment 2026-10-02).

When the session's agent calls ``ask_user``, the question reaches the client on
the turn's own stream, which only a client watching this session sees. Waypoint
publishes a Question entry on its session-events stream so that a client
following the session without watching its turn (a stories bar) learns of it
too. The agent's messages are made inside the sandbox, so the harness reports it
when it records the agent's ``ask_user`` message.

How a report is sent, and as whom, is sender.py's.

Reported once per turn, when the turn ends with the agent waiting on the user:
an ``ask_user`` call with no answer after it. Not when the call is first seen
-- a turn's first state carries the session's history, and a question already
answered must never be reported again.

"""

from core.waypoint_reports.sender import post_as_sandbox

QUESTIONS_ROUTE = "/internal/chat/questions"


def has_unanswered_question(messages: list) -> bool:
    """Whether the agent is waiting on the user: its state holds an ``ask_user``
    call (see ask_user.py) that no tool message answers.

    Decided over the whole serialized state (serialize_messages_for_values), so a
    question from earlier in the session, already answered, never counts.
    """
    answered = {
        m.get("tool_call_id") for m in messages if isinstance(m, dict) and m.get("type") == "tool"
    }
    for m in messages:
        if not isinstance(m, dict) or m.get("type") != "ai":
            continue
        for call in m.get("tool_calls") or []:
            if (
                isinstance(call, dict)
                and call.get("name") == "ask_user"
                and call.get("id") not in answered
            ):
                return True
    return False


def report_question(session_id: str) -> None:
    """Report an ``ask_user`` question to Waypoint, without waiting for it."""
    post_as_sandbox(QUESTIONS_ROUTE, session_id)
