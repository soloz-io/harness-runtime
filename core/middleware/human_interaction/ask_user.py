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
from pydantic import BaseModel, model_validator


class AskUserMedia(BaseModel):
    """A preview of what an option stands for."""

    kind: Literal["image", "audio", "video"]
    url: str

    aspect: Literal["square", "portrait", "landscape", "vertical"] = "square"
    """Preview shape: square 1:1, portrait 3:4, landscape 16:9, vertical 9:16."""

    poster: str | None = None
    """A still frame shown for a video until it is played."""


class AskUserOption(BaseModel):
    """A response choice shown as a card."""

    label: str
    """What the card is called — also the answer returned when it is chosen."""

    badge: str | None = None
    description: str | None = None
    media: AskUserMedia | None = None


class AskUserQuestion(BaseModel):
    """A single question to present to the user, used within the `questions` batch array."""

    question: str
    """The question text."""

    options: list[str | AskUserOption] | None = None
    """Optional list of predefined response choices."""

    layout: Literal["list", "carousel"] = "list"
    """How the options are shown: rows, or a row of preview cards."""

    blocking: bool | None = None
    """Whether this question blocks the workflow from continuing."""

    @model_validator(mode="after")
    def _carousel_options_have_media(self) -> "AskUserQuestion":
        if self.layout != "carousel":
            return self
        if not self.options:
            raise ValueError("a carousel question needs options")
        bare = [
            str(i)
            for i, o in enumerate(self.options)
            if not isinstance(o, AskUserOption) or o.media is None
        ]
        if bare:
            raise ValueError(f"carousel options need media; options without it: {', '.join(bare)}")
        return self


class AskUserInput(BaseModel):
    """The ask_user call. Validated before the tool runs, so a refusal reaches the agent as an error to correct."""

    questions: list[AskUserQuestion]
    type: Literal["approval", "clarification"] = "clarification"
    file_path: str | None = None

    @model_validator(mode="after")
    def _carousel_is_not_an_approval(self) -> "AskUserInput":
        # An approval dialog answers only "Approved": which card was chosen would be lost.
        if self.type == "approval" and any(q.layout == "carousel" for q in self.questions):
            raise ValueError(
                'a carousel question cannot be asked with type="approval": the answer would be '
                '"Approved" without the chosen option. Ask it again without type.'
            )
        return self


@tool("ask_user", args_schema=AskUserInput)
def ask_user(
    questions: list[AskUserQuestion],
    type: Literal["approval", "clarification"] = "clarification",
    file_path: str | None = None,
) -> str:
    """Relay questions to the user and wait for their response.

    Pauses execution and waits for the user to answer via the UI.

    Each question object has:
      - question (str): the question text
      - options (list, optional): predefined response choices. Each is a plain
        string, or an object {label, badge?, description?, media?} where media is
        {kind: "image"|"audio"|"video", url, aspect?: "square"|"portrait"|
        "landscape"|"vertical", poster?: still-frame url for a video}
      - layout ("list" | "carousel", default "list"): "carousel" shows the
        options as a row of preview cards the user can see or play before
        choosing; every option must then be an object with media, and `type`
        must not be "approval"
      - blocking (bool, optional): whether this blocks the workflow

    The answer for a chosen option is its label.

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
