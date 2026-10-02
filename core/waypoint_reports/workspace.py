"""
Workspace report — tells Waypoint that this session's workspace changed
(waypoint ADR-025, amendment 2026-10-02).

An agent's files are written to the sandbox's own disk. A client showing the
workspace -- an editor beside the chat -- has no way to know a file appeared
except by asking again, and asking on a timer is polling. So the harness says
when: every finished tool call lands in the agent's state as a tool message
naming the tool, the main agent's and every sub-agent's, and a tool that can
write to the workspace is the moment the workspace may have changed.

Read from state updates, not from the stream's tool events: a sub-agent's tool
calls do not arrive as tool events in this stream, so a hook there never saw
the writes that matter most -- the specialists'. Waypoint publishes a Files entry for the session, and the client reads
the file list once.

"May have changed", not "changed": a shell command that wrote nothing is
reported too. The cost is one file-list request, and telling the two apart
would mean watching the filesystem.

Throttled per session. The first change after a quiet period is reported at
once; changes within the window after it are remembered and reported together
by the next call, or when the turn ends -- so a burst of writes costs a couple
of reports, and the last write of a turn is never the one that goes unreported.
"""

import threading
import time

from core.waypoint_reports.sender import post_as_sandbox

FILES_CHANGED_ROUTE = "/internal/chat/files-changed"

# Tools that can write to the workspace. Reading and delegating tools are not
# here: a sub-agent's own writes are reported when ITS tools finish.
WORKSPACE_WRITING_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        # A shell command, a pack's CLI tool, the code interpreter: each can
        # write anything.
        "execute",
        "run_tool",
        "eval",
    }
)

# At most one report per session in this many seconds.
MIN_INTERVAL_SECONDS = 2.0

_lock = threading.Lock()
# session_id -> (when the last report was sent, whether a change is waiting)
_state: dict[str, tuple[float, bool]] = {}


def note_tool_finished(session_id: str, tool_name: str) -> None:
    """A tool call finished. If it can write to the workspace, report the change
    now, or remember it when one was reported moments ago."""
    if tool_name not in WORKSPACE_WRITING_TOOLS:
        return
    now = time.monotonic()
    with _lock:
        last, _pending = _state.get(session_id, (0.0, False))
        if last and now - last < MIN_INTERVAL_SECONDS:
            _state[session_id] = (last, True)
            return
        _state[session_id] = (now, False)
    post_as_sandbox(FILES_CHANGED_ROUTE, session_id)


def note_new_messages(session_id: str, messages: list) -> None:
    """New messages reached the agent's state (serialized, see
    serialize_messages_for_values). A tool message from a tool that can write is
    a workspace change."""
    for m in messages:
        if (
            isinstance(m, dict)
            and m.get("type") == "tool"
            and m.get("name") in WORKSPACE_WRITING_TOOLS
        ):
            note_tool_finished(session_id, m["name"])
            return


def flush_workspace_change(session_id: str) -> None:
    """The turn ended: report a change that was held back by the throttle."""
    with _lock:
        last, pending = _state.get(session_id, (0.0, False))
        if not pending:
            return
        _state[session_id] = (time.monotonic(), False)
    post_as_sandbox(FILES_CHANGED_ROUTE, session_id)
