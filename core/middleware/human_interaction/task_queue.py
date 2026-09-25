"""
Task Queue Tool — built-in non-blocking tool for reporting background job submission.

Unlike ``ask_user``, this tool does NOT call ``langgraph.types.interrupt()``.
It records the job metadata in a session-keyed slot and returns immediately,
allowing the orchestrator to continue without pausing.

After the current superstep's values event fires, ``RootValuesHandler`` drains
the calling session's pending payloads and emits a
``ResultFrame(subtype="task_queued")`` per job.  The playground UI picks these
up and adds them to its task queue panel, displaying a live progress indicator
that the user can monitor while the background job runs.

The client marks a task complete by polling the run status endpoint for the
job's ``run_id`` (``GET /api/v1/workflows/runs/{run_id}/status``); the
``[System Notification]`` in the chat stream is a separate, unrelated signal.

Session keying
--------------
Payloads are stored under the graph's ``configurable.thread_id``, which the
executor sets to the session id (``executor.py``: ``{"thread_id": session_id}``)
and ``langchain_core`` propagates to tool bodies via its runnable-config
contextvar (``ensure_config()``).  The HTTP server is multi-session by design —
``/session/{session_id}/message`` with a per-session ``SessionState`` — so a
process-global slot would let one session's job land on another session's SSE
stream the moment a process serves two sessions.  Calls made outside a graph
run (unit tests, direct invocation) fall back to ``__unscoped__`` and are
drained by the handler under the same key.
"""

import threading
from typing import Any, Optional

from langchain_core.runnables.config import ensure_config
from langchain_core.tools import tool

# ---------------------------------------------------------------------------
# Session-keyed payload store
# ---------------------------------------------------------------------------
# One list per session so a single superstep can queue several jobs (audio +
# video in one turn) without overwriting, and so concurrent sessions sharing a
# process never read each other's payloads.  The lock guards the dict itself —
# tool bodies may run on worker threads while the values handler drains from
# the event-loop thread.
# ---------------------------------------------------------------------------

UNSCOPED_KEY = "__unscoped__"
_lock = threading.Lock()
_pending: dict[str, list[dict[str, Any]]] = {}


def _session_key() -> str:
    """Key for the session running this tool call.

    Reads the runnable config LangGraph pushes onto the contextvar stack;
    ``thread_id`` IS the session id.  Outside a run there is no config, so the
    payload lands under ``UNSCOPED_KEY`` — the handler drains that same key,
    keeping direct invocations (tests) round-trippable.
    """
    try:
        config = ensure_config()
    except Exception:
        return UNSCOPED_KEY
    thread_id = (config.get("configurable") or {}).get("thread_id")
    return str(thread_id) if thread_id else UNSCOPED_KEY


def record_task_queue_payload(payload: dict[str, Any]) -> None:
    """Append a job payload to the current session's pending list."""
    key = _session_key()
    with _lock:
        _pending.setdefault(key, []).append(payload)


def consume_task_queue_payloads(session_key: str) -> list[dict[str, Any]]:
    """Return and clear every pending payload for ``session_key``.

    Called by ``RootValuesHandler`` after each values event so each payload is
    emitted exactly once.  Returns ``[]`` when nothing is pending.
    """
    with _lock:
        return _pending.pop(session_key, [])


def _clear_all_payloads() -> None:
    """Drop every pending payload (test helper)."""
    with _lock:
        _pending.clear()


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


@tool("task_queue")
def task_queue(
    job_name: str,
    run_id: Optional[str] = None,
    job_id: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Report a background job to the task queue panel in the playground UI.

    Call this immediately after a specialist reports a QUEUED job (i.e. when
    the specialist's tool output contains ``"status": "QUEUED"`` with a
    ``run_id``).  The playground UI will display a live 'task running'
    indicator and poll the job until it completes.

    Unlike ``ask_user``, this tool is **non-blocking** — the graph continues
    immediately after this call.  No user action is required to resume.

    Args:
        job_name: Human-readable name shown in the task panel
                  (e.g. "Generating voice-over track").
        run_id: The Waypoint ``run_id`` returned by the CLI tool.  The client
                uses this to poll run status until the job finishes.
        job_id: Alias for ``run_id`` — either may be supplied.
        description: Optional longer description shown below ``job_name`` in
                     the panel (e.g. "This may take a few minutes.").

    Returns:
        Acknowledgment string confirming the task was registered in the UI.
    """
    effective_run_id = run_id or job_id
    record_task_queue_payload(
        {
            "job_name": job_name,
            "run_id": effective_run_id,
            "description": description,
        }
    )
    parts = [f"Task '{job_name}'"]
    if effective_run_id:
        parts.append(f"(run_id={effective_run_id})")
    parts.append("added to the UI task queue.")
    return " ".join(parts)
