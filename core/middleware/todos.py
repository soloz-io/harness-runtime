"""
A todo list any agent can keep: the ``write_todos`` tool, and nothing else.

The user watches each agent's todo list as the progress of a request (waypoint
ADR-052): the harness publishes an agent's ``todos`` state live, and the browser
reads ``write_todos`` calls back from history. This middleware provides exactly
that tool and that state.

It replaces LangChain's ``TodoListMiddleware``, which deepagents stopped adding
in 0.7.0. That middleware also appends a planning prompt to every model call
and gives the tool a ~4,000-character description, both telling the model when
not to plan; the added text changed how agents behaved. This one adds no prompt,
keeps the description to what the tool does, and answers each call with one
line instead of echoing the list back. When to plan is the agent's own
instructions' decision.
"""

from typing import Annotated, Any, Literal

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import OmitFromInput
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field
from typing_extensions import NotRequired, TypedDict


class Todo(TypedDict):
    content: str
    status: Literal["pending", "in_progress", "completed"]


class TodoState(AgentState):
    # Read by the harness's publisher (SubagentValuesHandler) by this key.
    todos: Annotated[NotRequired[list[Todo]], OmitFromInput]


class TodoItem(BaseModel):
    content: str = Field(min_length=1, description="What this item is, in the user's words.")
    status: Literal["pending", "in_progress", "completed"]


class WriteTodosInput(BaseModel):
    todos: list[TodoItem] = Field(
        description="The whole list, in order. It replaces the previous one."
    )
    tool_call_id: Annotated[str, InjectedToolCallId]


DESCRIPTION = (
    "Write your todo list for the task you are doing. Each call carries the whole list and "
    "replaces the one before; each item has content and a status: pending, in_progress or "
    "completed. The user sees this list as the progress of their request."
)


def _write_todos(todos: list[TodoItem], tool_call_id: str) -> Command[Any]:
    return Command(
        update={
            "todos": [
                item.model_dump() if isinstance(item, BaseModel) else dict(item) for item in todos
            ],
            "messages": [ToolMessage("Todo list updated.", tool_call_id=tool_call_id)],
        }
    )


class TodoMiddleware(AgentMiddleware):
    """Gives an agent ``write_todos`` and a ``todos`` state; adds nothing to its prompt."""

    state_schema = TodoState

    def __init__(self) -> None:
        super().__init__()
        self.tools = [
            StructuredTool.from_function(
                name="write_todos",
                description=DESCRIPTION,
                func=_write_todos,
                args_schema=WriteTodosInput,
                infer_schema=False,
            )
        ]
