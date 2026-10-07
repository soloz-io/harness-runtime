"""Background jobs reach the agent through its own pending jobs (ADR-015).

An agent that starts a job holds the run's id in ``pending_jobs``, a field of
its graph state. Before each model call, the main agent looks those runs up in
``workflow_runs`` and is told about the ones that have finished. Nothing is
pushed at the agent: a job's notice only wakes an idle session and shows the job
in the chat, so a job outcome is never user input and never a resume value.

- ``JobTrackingMiddleware`` (every agent) adds the run id of each job a tool
  result reports as started. A subagent's ids reach the main agent through
  deepagents' own state passing: the subagent returns its state when it
  finishes.
- ``JobReportingMiddleware`` (the main agent only) reports finished jobs and
  removes their ids, both in one checkpoint.
"""

import asyncio
import json
from typing import Annotated, Any, Awaitable, Callable, Iterator, NotRequired, Optional

import structlog
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

logger = structlog.get_logger(__name__)

SYSTEM_NOTICE_PREFIX = "[System Notification]"

# Marks a message as a reported job outcome: the agent reads it, the chat does
# not show it (the chat already shows the job's own notice row).
JOB_OUTCOME_KEY = "job_outcome"

_FINISHED_STATUSES = ("failed", "cancelled")


def merge_pending_jobs(current: Optional[list[str]], update: Any) -> list[str]:
    """``pending_jobs`` merges rather than being overwritten.

    A list -- from a tool result, or a subagent returning its state -- is
    unioned with the current ids, so subagents running in parallel in one step
    each add theirs, and a subagent echoing back the ids it was given changes
    nothing. ``{"remove": [...]}`` takes ids out.
    """
    ids = list(current or [])
    if isinstance(update, dict):
        removed = set(update.get("remove") or [])
        return [run_id for run_id in ids if run_id not in removed]
    for run_id in update or []:
        if run_id not in ids:
            ids.append(run_id)
    return ids


class BackgroundJobsState(AgentState):
    pending_jobs: NotRequired[Annotated[list[str], merge_pending_jobs]]


# ── Job tracking ────────────────────────────────────────────────────────────


def _json_objects(text: str) -> Iterator[dict[str, Any]]:
    """The JSON objects in a tool's text: the whole text, a dispatcher's
    ``output`` field inside it, or one object per line of a CLI's output."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        value = None
    if isinstance(value, dict):
        yield value
        output = value.get("output")
        if isinstance(output, str):
            yield from _json_objects(output)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def started_job(message: ToolMessage) -> Optional[str]:
    """The run id of the job a tool result reports as started, or None.

    The platform contract: a JSON object with ``status: "QUEUED"`` and the
    ``run_id`` the SDK's ``runs/start`` returned.
    """
    content = message.content
    if isinstance(content, list):
        content = "\n".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    if not isinstance(content, str):
        return None
    for obj in _json_objects(content):
        run_id = obj.get("run_id")
        if obj.get("status") == "QUEUED" and isinstance(run_id, str) and run_id:
            return run_id
    return None


def _track(result: ToolMessage | Command) -> ToolMessage | Command:
    if not isinstance(result, ToolMessage):
        # A Command (a subagent returning through `task`) carries its own state
        # update, its pending jobs included.
        return result
    run_id = started_job(result)
    if run_id is None:
        return result
    logger.info("background_job_tracked", run_id=run_id, tool=result.name)
    return Command(update={"messages": [result], "pending_jobs": [run_id]})


class JobTrackingMiddleware(AgentMiddleware):
    """Adds the run id of each job a tool result reports as started."""

    state_schema = BackgroundJobsState

    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], ToolMessage | Command],
    ) -> ToolMessage | Command:
        return _track(handler(request))

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        return _track(await handler(request))


# ── Job reporting ───────────────────────────────────────────────────────────


def _pool() -> Any:
    from core.services import get_services

    manager = get_services().execution_manager
    return getattr(manager, "_pool", None) if manager is not None else None


def read_runs(pool: Any, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Status, error, outcome and name of each run, by id."""
    if not run_ids:
        return {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, status, error, outcome, workflow_name FROM workflow_runs WHERE id = ANY(%s)",
                (run_ids,),
            )
            return {
                row[0]: {"status": row[1], "error": row[2], "outcome": row[3], "name": row[4]}
                for row in cur.fetchall()
            }


def is_finished(run: Optional[dict[str, Any]]) -> bool:
    """A run is finished when its outcome is recorded, or it failed or was
    cancelled, or it no longer exists.

    Not by the completed status alone: a run is marked completed after its
    notify step, and the wake-up that step causes can reach the agent first.
    """
    if run is None:
        return True
    return bool(run.get("outcome")) or run.get("status") in _FINISHED_STATUSES


def outcome_fields(run: Optional[dict[str, Any]]) -> dict[str, Any]:
    """What the agent is told about a finished run."""
    if run is None:
        return {"type": "job/failed", "error": "The job can't be found."}
    if run.get("outcome"):
        return dict(run["outcome"])
    if run.get("status") == "cancelled":
        return {
            "type": "job/cancelled",
            "message": "The job was cancelled.",
            "title": run.get("name") or "",
        }
    return {
        "type": "job/failed",
        "error": run.get("error") or "The job failed.",
        "title": run.get("name") or "",
    }


def outcome_text(fields: dict[str, Any]) -> str:
    """``[System Notification] <message>``, then the outcome's other fields --
    the form the agents' instructions describe."""
    lead_key = next((k for k in ("message", "error", "title") if fields.get(k)), None)
    lead = str(fields[lead_key]) if lead_key else ""
    lines: list[str] = []
    for key, value in fields.items():
        if key in (lead_key, "run_id") or value in (None, ""):
            continue
        rendered = (value if isinstance(value, str) else json.dumps(value)).strip()
        if not rendered or rendered in lead:
            continue
        lines.append(f"{key}: {rendered}")
    return f"{SYSTEM_NOTICE_PREFIX} " + "\n".join(([lead] if lead else []) + lines)


def outcome_message(run_id: str, run: Optional[dict[str, Any]]) -> HumanMessage:
    """A reported outcome, in the user position the model API needs, marked as
    a job outcome so no harness path treats it as human input."""
    return HumanMessage(
        content=outcome_text(outcome_fields(run)),
        id=f"job-outcome-{run_id}",
        additional_kwargs={JOB_OUTCOME_KEY: {"run_id": run_id}},
    )


def finished_jobs(pool: Any, pending: list[str]) -> list[tuple[str, Optional[dict[str, Any]]]]:
    """The pending jobs that have finished, in the order they were started."""
    runs = read_runs(pool, pending)
    return [(run_id, runs.get(run_id)) for run_id in pending if is_finished(runs.get(run_id))]


def _pending(state: AgentState) -> list[str]:
    """The run ids in ``pending_jobs`` (BackgroundJobsState, declared by both
    middlewares), read from the agent state the hook is given."""
    return list(state.get("pending_jobs") or [])


class JobReportingMiddleware(AgentMiddleware):
    """Before each model call, tells the main agent about its finished jobs."""

    state_schema = BackgroundJobsState

    def before_model(self, state: AgentState, runtime: Runtime[Any]) -> Optional[dict[str, Any]]:
        pending = _pending(state)
        pool = _pool()
        if not pending or pool is None:
            return None
        return self._report(finished_jobs(pool, pending))

    async def abefore_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> Optional[dict[str, Any]]:
        pending = _pending(state)
        pool = _pool()
        if not pending or pool is None:
            return None
        return self._report(await asyncio.to_thread(finished_jobs, pool, pending))

    @staticmethod
    def _report(finished: list[tuple[str, Optional[dict[str, Any]]]]) -> Optional[dict[str, Any]]:
        if not finished:
            return None
        logger.info("background_jobs_reported", run_ids=[run_id for run_id, _ in finished])
        return {
            "messages": [outcome_message(run_id, run) for run_id, run in finished],
            "pending_jobs": {"remove": [run_id for run_id, _ in finished]},
        }


def is_job_outcome(message: dict[str, Any]) -> bool:
    """Whether a serialized message is a reported job outcome."""
    kwargs = message.get("additional_kwargs") or {}
    return isinstance(kwargs, dict) and JOB_OUTCOME_KEY in kwargs
