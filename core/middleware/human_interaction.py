"""
Human Interaction Middleware — provides all HITL tools to agents.

Groups `ask_user` under a single middleware so agents
only need one middleware entry in the stack. Wire this into all topology
builders (star, acrylic, subagent). The tools' bodies are never executed —
they are intercepted by `HumanInTheLoopMiddleware` via `interrupt_on`.
"""

from langchain.agents.middleware import AgentMiddleware

from core.ask_user import ask_user


class HumanInteractionMiddleware(AgentMiddleware):
    """Provides all human-interaction tools to agents.

    Wire this middleware into the agent's middleware stack to make
    `ask_user` available. Their actual behavior
    comes from the `HumanInTheLoopMiddleware` configured via `interrupt_on`.
    """

    tools = [ask_user]
