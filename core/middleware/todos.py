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

One check, at the end: an agent that gives its final answer while its list
still has open items is sent back to the model once, with a notice naming
them, to close the list. Agents wrote a plan and finished without marking it
done, leaving the user's task list running. Nothing is added to the prompt
otherwise, and it fires at most once per run, so it can never loop. The notice
is marked (``TODO_REMINDER_KEY``), like a reported job outcome: it is the
agent's to read, never stored or shown as a chat message.
"""

import uuid
from typing import Annotated, Any, Literal

import structlog
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import OmitFromInput, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, StructuredTool
from langgraph.types import Command
from pydantic import BaseModel, Field
from typing_extensions import NotRequired, TypedDict

from core.middleware.background_jobs.middleware import JOB_OUTCOME_KEY

logger = structlog.get_logger(__name__)

TODO_REMINDER_KEY = "todo_reminder"


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


def is_todo_reminder(message: Any) -> bool:
    """Whether a message (live or serialized) is the end-of-run todo notice."""
    kwargs = (
        message.get("additional_kwargs")
        if isinstance(message, dict)
        else getattr(message, "additional_kwargs", None)
    ) or {}
    return isinstance(kwargs, dict) and TODO_REMINDER_KEY in kwargs


def _reminded_this_run(messages: list[Any]) -> bool:
    """Whether the notice was already given since the run's input.

    A run's input is the last human message that is not a harness notice (a
    reminder or a reported job outcome, both in the user position the model
    API needs). A subagent's run starts from its task message.
    """
    for message in reversed(messages):
        if is_todo_reminder(message):
            return True
        if isinstance(message, HumanMessage) and JOB_OUTCOME_KEY not in (
            message.additional_kwargs or {}
        ):
            return False
    return False


def _reminder(open_items: list[dict[str, Any]]) -> HumanMessage:
    items = "\n".join(f"- [{t.get('status')}] {t.get('content')}" for t in open_items)
    return HumanMessage(
        content=(
            "[System Notification] Your todo list still has open items:\n"
            f"{items}\n"
            "Before you finish, call write_todos once more with the whole list: mark each "
            "item you have done completed, and remove any you did not do. Then give your "
            "final answer again."
        ),
        id=f"todo-reminder-{uuid.uuid4().hex}",
        additional_kwargs={TODO_REMINDER_KEY: True},
    )


class TodoMiddleware(AgentMiddleware):
    """Gives an agent ``write_todos`` and a ``todos`` state; adds nothing to its prompt.

    After the model's final answer, sends the agent back once to close a list
    it left open.
    """

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

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:  # noqa: ARG002
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        # Only a final answer: a reply with tool calls (valid or not) is still working.
        if not isinstance(last, AIMessage) or last.tool_calls or last.invalid_tool_calls:
            return None
        open_items = [t for t in state.get("todos") or [] if t.get("status") != "completed"]
        if not open_items or _reminded_this_run(messages):
            return None
        logger.info("todo_list_left_open", open_items=len(open_items))
        return {"messages": [_reminder(open_items)], "jump_to": "model"}

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
