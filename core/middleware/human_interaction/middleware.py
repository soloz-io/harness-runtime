"""
Human Interaction Middleware — provides the user-facing tools to agents.

Groups them under a single middleware so agents need one entry in the stack.
Wire this into all topology builders (star, acrylic, subagent).

`ask_user` is intercepted by `HumanInTheLoopMiddleware` via `interrupt_on`, so
its body never runs — a question has no answer until the graph stops and someone
gives one.

`task_queue` is non-blocking and runs its body directly — it records a pending
job payload that ``RootValuesHandler`` drains and emits as a ``task_queued``
result frame.  No interrupt or human decision is required.
"""

from langchain.agents.middleware import AgentMiddleware

from core.middleware.human_interaction.ask_user import ask_user
from core.middleware.human_interaction.task_queue import task_queue


class HumanInteractionMiddleware(AgentMiddleware):
    """Provides the human-interaction tools to agents.

    `ask_user`'s behaviour comes from `HumanInTheLoopMiddleware` via
    `interrupt_on`.  `task_queue` is non-blocking and executes immediately.
    """

    tools = [ask_user, task_queue]
