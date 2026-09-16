"""
Human Interaction Middleware — provides the user-facing tools to agents.

Groups them under a single middleware so agents need one entry in the stack.
Wire this into all topology builders (star, acrylic, subagent).

`ask_user` is intercepted by `HumanInTheLoopMiddleware` via `interrupt_on`, so
its body never runs — a question has no answer until the graph stops and someone
gives one.
"""

from langchain.agents.middleware import AgentMiddleware

from core.middleware.human_interaction.ask_user import ask_user


class HumanInteractionMiddleware(AgentMiddleware):
    """Provides the human-interaction tools to agents.

    `ask_user`'s behaviour comes from `HumanInTheLoopMiddleware` via
    `interrupt_on`.
    """

    tools = [ask_user]
