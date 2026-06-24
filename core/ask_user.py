"""
Ask User Tool — built-in HITL tool for relaying questions to the user.

Registered on the `HumanInteractionMiddleware` alongside `review_content`.
The tool body is never executed — it is intercepted by
`HumanInTheLoopMiddleware` via `interrupt_on`. The human's response is
injected as the tool result via the `respond` decision.
"""

from langchain_core.tools import tool
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
def ask_user(questions: list[AskUserQuestion]) -> str:
    """Relay questions to the user and wait for their response.

    Pauses execution and waits for the user to answer via the UI.

    Each question object has:
      - question (str): the question text
      - options (list[str], optional): predefined response choices
      - blocking (bool, optional): whether this blocks the workflow

    Args:
        questions: Array of question objects to present to the user.

    Returns:
        The text of the user's response.
    """
    # The body is a no-op!
    # The actual response is injected by the HumanInTheLoopMiddleware
    # when the human submits their answer in the UI.
    return ""
