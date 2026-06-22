"""
Ask User Tool — built-in HITL tool for relaying questions to the user.

Provides the `ask_user` tool via `AskUserMiddleware`, which is wired into
all topology builders (star, acrylic, subagent). The tool body is never
executed — it is intercepted by `HumanInTheLoopMiddleware` via `interrupt_on`.
The human's response is injected as the tool result via the `respond` decision.
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool


@tool("ask_user")
def ask_user(question: str, options: list[str] | None = None, blocking: bool = False) -> str:
    """Relay a question to the user and wait for their response.

    Pauses execution and waits for the user to answer via the UI.

    Args:
        question: The question or message to present to the user.
        options: Optional list of predefined response choices.
        blocking: Whether this question is blocking the workflow from continuing.

    Returns:
        The text of the user's response.
    """
    # The body is a no-op!
    # The actual response is injected by the HumanInTheLoopMiddleware
    # when the human submits their answer in the UI.
    return ""


class AskUserMiddleware(AgentMiddleware):
    """Provides the ask_user tool to agents.

    Wire this middleware into the agent's middleware stack to make the
    ask_user tool available. The tool's actual behavior comes from the
    HumanInTheLoopMiddleware configured via interrupt_on.
    """

    tools = [ask_user]
