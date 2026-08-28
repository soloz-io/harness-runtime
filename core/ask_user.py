"""
Ask User Tool — built-in HITL tool for relaying questions to the user.

The tool body calls ``langgraph.types.interrupt()`` directly, so the graph
always pauses at an active LangGraph interrupt when ``ask_user`` is invoked —
regardless of whether the workflow definition includes ``interrupt_on`` config.

On resume, LangGraph returns the resume value (the decisions payload) as the
return value of ``interrupt()``, which is then returned as the tool result.
The harness event publisher reads ``__interrupt__`` from the checkpoint stream
to emit the ``action_requests`` / ``review_configs`` interrupt event to the UI.
"""

from typing import Any, Literal

from langchain_core.tools import tool
from langgraph.types import interrupt
from pydantic import BaseModel


class AskUserQuestion(BaseModel):
    """A single question to present to the user, used within the `questions` batch array."""

    question: str
    """The question text."""

    options: list[str] | None = None
    """Optional list of predefined response choices."""

    blocking: bool | None = None
    """Whether this question blocks the workflow from continuing."""


@tool("ask_user")
def ask_user(
    questions: list[AskUserQuestion],
    type: Literal["approval", "clarification"] = "clarification",
    file_path: str | None = None,
) -> str:
    """Relay questions to the user and wait for their response.

    Pauses execution and waits for the user to answer via the UI.

    Each question object has:
      - question (str): the question text
      - options (list[str], optional): predefined response choices
      - blocking (bool, optional): whether this blocks the workflow

    Args:
        questions: Array of question objects to present to the user.
        type: 'approval' if asking for phase approval, 'clarification' for discovery questions.
        file_path: Optional path to a file to display alongside the question.

    Returns:
        The text of the user's response (the ``respond`` decision's message).
    """
    # Pause the graph at a real LangGraph interrupt. The interrupt value is
    # the full ask_user payload so the harness event publisher can build the
    # action_requests / review_configs interrupt event for the UI.
    resume_value: Any = interrupt(
        {
            "name": "ask_user",
            "args": {
                "questions": [q.model_dump() for q in questions],
                "type": type,
                "file_path": file_path,
            },
        }
    )

    # resume_value is whatever was passed to Command(resume=...) by the SDK.
    # Extract the human's text from the decisions payload if present.
    if isinstance(resume_value, dict):
        decisions = resume_value.get("decisions", [])
        if decisions and isinstance(decisions, list):
            first = decisions[0]
            if isinstance(first, dict):
                return str(first.get("message", ""))
    return str(resume_value) if resume_value is not None else ""
