"""
Reply report — tells Waypoint that this session's agent replied to the user
(waypoint ADR-025, amendment 2026-10-09).

The agent's reply is written inside the sandbox, to the turn's own stream and to
``chat_messages``. A client following the session without watching its turn (a
stories bar badging unread sessions, waypoint ADR-049) learns of it only if
Waypoint publishes it.

A reply is a message of the main agent with text for the user. Sub-agents'
messages, tool results and the agent's tool-only messages are not.

"New" is what the session had not recorded before: the message store skips a
message it already holds, so a turn's first state, which carries the whole
history, never counts. Reported once, when the turn ends, and not when the turn
ends on an unanswered question: that is reported as a question.

How a report is sent, and as whom, is sender.py's.
"""

from typing import Any

from core.waypoint_reports.sender import post_as_sandbox

REPLIES_ROUTE = "/internal/chat/replies"


def _has_text(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and bool(str(block.get("text", "")).strip())
            for block in content
        )
    return False


def is_reply(message: Any) -> bool:
    """Whether a serialized message is the main agent's text for the user."""
    return (
        isinstance(message, dict)
        and message.get("type") == "ai"
        and _has_text(message.get("content"))
    )


def report_reply(session_id: str) -> None:
    """Report the agent's reply to Waypoint, without waiting for it."""
    post_as_sandbox(REPLIES_ROUTE, session_id)
