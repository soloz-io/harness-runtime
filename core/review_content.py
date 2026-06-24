"""
Review Content Tool — built-in HITL tool for phase output review and approval.

Registered on the `HumanInteractionMiddleware` alongside `ask_user`.
The tool body is never executed — it is intercepted by
`HumanInTheLoopMiddleware` via `interrupt_on`. The human's approve/reject/edit
decision is injected as the tool result via the resume decisions.
"""

from langchain_core.tools import tool


@tool("review_content")
def review_content(phase_name: str, content: str) -> str:
    """Request human review and approval of completed phase output.

    Presents the phase deliverable (content) to the user for review.
    The user can approve, reject with feedback, or edit the content before
    continuing.

    Args:
        phase_name: Name of the phase whose output is being reviewed.
        content: The phase deliverable content to present for review.

    Returns:
        The user's decision and any associated feedback message.
    """
    return ""
