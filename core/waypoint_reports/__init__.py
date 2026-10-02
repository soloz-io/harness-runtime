"""
Reports from a sandbox to Waypoint (waypoint ADR-025, amendment 2026-10-02).

Some things happen only inside the sandbox, and a client following the session
without watching its turn learns of them only if Waypoint publishes them. The
harness reports each to the SDK, which publishes a session entry for it.

    sender.py     how a report is sent, and as whom (the sandbox's identity)
    questions.py  the agent asked the user a question (Question entry)
    workspace.py  the workspace may have changed (Files entry)
"""

from core.waypoint_reports.questions import has_unanswered_question, report_question
from core.waypoint_reports.workspace import (
    flush_workspace_change,
    note_new_messages,
    note_tool_finished,
)

__all__ = [
    "flush_workspace_change",
    "has_unanswered_question",
    "note_new_messages",
    "note_tool_finished",
    "report_question",
]
