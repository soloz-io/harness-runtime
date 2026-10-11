"""A session's computer reports to its agent the way a finished job does (ADR-015).

A computer app (waypoint ADR-050) can see what the agent cannot: what the work
does when it runs in front of the user. It reports that through the page as a
system notice of type ``computer/report``, with a ``title`` for the chat and a
``message`` for the agent. The notice is written to the chat like any other, so
the user sees it -- and a notice is the chat's, not the graph's: on its own the
agent would never read it.

So the notice row is also the report's record. The main agent holds, in
``computer_reports_seen``, the sequence of the last notice row it has read.
Before each model call it reads the rows after that one and is given every
report among them, in the user position the model API needs, marked so the
chat does not show the report twice. A report that arrives while the agent is
idle wakes it (the executor's wake check); one that arrives mid-run is read at
its next model call; one that arrives while a question is open waits for the
answer, which is never taken from a report.
"""

import asyncio
import json
from typing import Any, NotRequired, Optional

import structlog
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langgraph.config import get_config
from langgraph.runtime import Runtime

from core.middleware.background_jobs.middleware import SYSTEM_NOTICE_PREFIX

logger = structlog.get_logger(__name__)

COMPUTER_REPORT_TYPE = "computer/report"

# Marks a message as a delivered report: the agent reads it, the chat does not
# show it (the chat already shows the notice row).
COMPUTER_REPORT_KEY = "computer_report"

SEEN_KEY = "computer_reports_seen"


class ComputerReportsState(AgentState):
    computer_reports_seen: NotRequired[int]


def report_message_text(content: Any) -> Optional[str]:
    """The agent's text of a notice that is a computer report, else None."""
    if not isinstance(content, str):
        return None
    try:
        notice = json.loads(content)
    except ValueError:
        return None
    if not isinstance(notice, dict) or notice.get("type") != COMPUTER_REPORT_TYPE:
        return None
    message = notice.get("message")
    return message if isinstance(message, str) and message.strip() else None


def unread_reports(
    pool: Any, session_id: str, seen: int
) -> tuple[list[tuple[int, str]], Optional[int]]:
    """The reports among the session's notice rows after ``seen``, and the
    sequence of the last notice row read (None when there is none)."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sequence, content FROM chat_messages
                WHERE session_id = %s AND source = 'notification' AND sequence > %s
                ORDER BY sequence
                """,
                (session_id, seen),
            )
            rows = cur.fetchall()
    reports = [(seq, text) for seq, content in rows if (text := report_message_text(content))]
    return reports, (rows[-1][0] if rows else None)


def has_unread_reports(pool: Any, session_id: str, seen: int) -> bool:
    """Whether the session has a report its agent has not read: what the
    executor's wake check asks."""
    reports, _ = unread_reports(pool, session_id, seen)
    return bool(reports)


def report_message(sequence: int, text: str) -> HumanMessage:
    """A delivered report, marked so no harness path treats it as human input."""
    return HumanMessage(
        content=f"{SYSTEM_NOTICE_PREFIX} {text}",
        id=f"computer-report-{sequence}",
        additional_kwargs={COMPUTER_REPORT_KEY: {"sequence": sequence}},
    )


def is_computer_report(message: dict[str, Any]) -> bool:
    """Whether a serialized message is a delivered computer report."""
    kwargs = message.get("additional_kwargs") or {}
    return isinstance(kwargs, dict) and COMPUTER_REPORT_KEY in kwargs


def _session_id() -> Optional[str]:
    configurable = (get_config() or {}).get("configurable") or {}
    session_id = configurable.get("thread_id")
    return session_id if isinstance(session_id, str) and session_id else None


def _pool() -> Any:
    from core.services import get_services

    manager = get_services().execution_manager
    return getattr(manager, "_pool", None) if manager is not None else None


class ComputerReportMiddleware(AgentMiddleware):
    """Before each model call, gives the main agent its computer's new reports."""

    state_schema = ComputerReportsState

    def before_model(self, state: AgentState, runtime: Runtime[Any]) -> Optional[dict[str, Any]]:
        pool, session_id = _pool(), _session_id()
        if pool is None or session_id is None:
            return None
        return self._deliver(*unread_reports(pool, session_id, int(state.get(SEEN_KEY, -1))))

    async def abefore_model(
        self, state: AgentState, runtime: Runtime[Any]
    ) -> Optional[dict[str, Any]]:
        pool, session_id = _pool(), _session_id()
        if pool is None or session_id is None:
            return None
        found = await asyncio.to_thread(
            unread_reports, pool, session_id, int(state.get(SEEN_KEY, -1))
        )
        return self._deliver(*found)

    @staticmethod
    def _deliver(reports: list[tuple[int, str]], last: Optional[int]) -> Optional[dict[str, Any]]:
        if last is None:
            return None
        # Every notice row read moves the mark, reports or not, so the next
        # model call reads only what is new.
        update: dict[str, Any] = {SEEN_KEY: last}
        if reports:
            logger.info("computer_reports_delivered", sequences=[seq for seq, _ in reports])
            update["messages"] = [report_message(seq, text) for seq, text in reports]
        return update
