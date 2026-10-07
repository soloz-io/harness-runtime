"""
Human Interaction Middleware — provides `ask_user`, the tool that asks a person.

Only the agent the user talks to gets it: the orchestrator of a composite or
star topology, or a standalone graph node. A specialist reaches the user only
through its Decision Report (the orchestrator relays its questions); given
`ask_user`, its question would be answered to the orchestrator, which never saw
it, and the specialist's own context — the options it offered — would be lost.

`ask_user` pauses the graph at a native LangGraph interrupt (see ask_user.py);
only Command(resume=...) continues it. System notices never enter the graph --
the harness writes them to the chat (api/routers/sessions.py) -- so one can
neither answer nor cancel the open question.

A background job needs no tool: its workflow's own notices tell the chat it
started and finished.
"""

from langchain.agents.middleware import AgentMiddleware

from core.middleware.human_interaction.ask_user import ask_user


class HumanInteractionMiddleware(AgentMiddleware):
    """Provides `ask_user` to the agent the user talks to.

    Its behaviour comes from `HumanInTheLoopMiddleware` via `interrupt_on`.
    """

    tools = [ask_user]
