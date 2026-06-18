import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain.tools import InjectedToolCallId
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.types import Command, interrupt

from core.ask_user_types import AskUserRequest, Question

logger = logging.getLogger(__name__)


ASK_USER_TOOL_DESCRIPTION = """Ask the user one or more questions when you need clarification or input before proceeding.

Each question can be either:
- "text": Free-form text response from the user
- "multiple_choice": User selects from predefined options (an "Other" option is always available)

For multiple choice questions, provide a list of choices. The user can pick one or type a custom answer via the "Other" option.

By default all questions are required. Set "required" to false for optional questions that the user can skip. Do not include "(required)", "(optional)", "- optional", or similar annotations in the question text — the UI renders that separately based on the "required" field.

Use this tool when:
- You need clarification on ambiguous requirements
- You want the user to choose between multiple valid approaches
- You need specific information only the user can provide
- You want to confirm a plan before executing it

Do NOT use this tool for:
- Simple yes/no confirmations (just proceed with your best judgment)
- Questions you can answer yourself from context
- Trivial decisions that don't meaningfully affect the outcome"""

ASK_USER_SYSTEM_PROMPT = """## `ask_user`

You have access to the `ask_user` tool to ask the user questions when you need clarification or input.
Use this tool sparingly - only when you genuinely need information from the user that you cannot determine from context.

When using `ask_user`:
- Be concise and specific with your questions
- Use multiple choice when there are clear options to choose from
- Use text input when you need free-form responses
- Group related questions into a single ask_user call rather than making multiple calls
- Never ask questions you can answer yourself from the available context"""


def _validate_questions(questions: list[Question]) -> None:
    if not questions:
        msg = "ask_user requires at least one question"
        raise ValueError(msg)

    for q in questions:
        question_text = q.get("question")
        if not isinstance(question_text, str) or not question_text.strip():
            msg = "ask_user questions must have non-empty 'question' text"
            raise ValueError(msg)

        question_type = q.get("type")
        if question_type not in {"text", "multiple_choice"}:
            msg = f"unsupported ask_user question type: {question_type!r}"
            raise ValueError(msg)

        if question_type == "multiple_choice" and not q.get("choices"):
            msg = f"multiple_choice question {q.get('question')!r} requires a non-empty 'choices' list"
            raise ValueError(msg)

        if question_type == "text" and q.get("choices"):
            msg = f"text question {q.get('question')!r} must not define 'choices'"
            raise ValueError(msg)


def _parse_answers(
    response: object,
    questions: list[Question],
    tool_call_id: str,
) -> Command[Any]:
    status: str = "answered"
    error_text: str | None = None
    answers: list[str]
    if not isinstance(response, dict):
        logger.error(
            "ask_user received malformed resume payload (expected dict, got %s); returning explicit error answers",
            type(response).__name__,
        )
        answers = []
        status = "error"
        error_text = "invalid ask_user response payload"
    else:
        response_dict = cast("dict[str, Any]", response)
        response_status = response_dict.get("status")
        if isinstance(response_status, str):
            status = response_status

        if "answers" not in response_dict:
            if status == "answered":
                logger.error("ask_user received resume payload without 'answers'; returning explicit error answers")
                answers = []
                status = "error"
                error_text = "missing ask_user answers payload"
            else:
                answers = []
        else:
            raw_answers = response_dict["answers"]
            if isinstance(raw_answers, list):
                answers = [str(answer) for answer in raw_answers]
            else:
                logger.error("ask_user received non-list 'answers' payload (%s); returning explicit error answers", type(raw_answers).__name__)
                answers = []
                status = "error"
                error_text = "invalid ask_user answers payload"

        if status == "error":
            response_error = response_dict.get("error")
            if isinstance(response_error, str) and response_error:
                error_text = response_error
        elif status == "cancelled":
            answers = ["(cancelled)" for _ in questions]
        elif status == "answered":
            if len(answers) != len(questions):
                logger.warning("ask_user answer count mismatch: expected %d, got %d", len(questions), len(answers))
        else:
            logger.error("ask_user received unknown status %r; returning explicit error answers", status)
            answers = []
            status = "error"
            error_text = "invalid ask_user response status"

    if status == "error":
        detail = error_text or "ask_user interaction failed"
        answers = [f"(error: {detail})" for _ in questions]

    formatted_answers = []
    for i, q in enumerate(questions):
        answer = answers[i] if i < len(answers) else "(no answer)"
        formatted_answers.append(f"Q: {q['question']}\nA: {answer}")
    result_text = "\n\n".join(formatted_answers)
    return Command(
        update={
            "messages": [ToolMessage(result_text, tool_call_id=tool_call_id)],
        }
    )


class AskUserMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    def __init__(
        self,
        *,
        system_prompt: str = ASK_USER_SYSTEM_PROMPT,
        tool_description: str = ASK_USER_TOOL_DESCRIPTION,
    ) -> None:
        super().__init__()
        self.system_prompt = system_prompt
        self.tool_description = tool_description

        @tool(description=self.tool_description)
        def _ask_user(
            questions: list[Question],
            tool_call_id: Annotated[str, InjectedToolCallId],
        ) -> Command[Any]:
            _validate_questions(questions)
            ask_request = AskUserRequest(
                type="ask_user",
                questions=questions,
                tool_call_id=tool_call_id,
            )
            response = interrupt(ask_request)
            return _parse_answers(response, questions, tool_call_id)

        _ask_user.name = "ask_user"
        self.tools = [_ask_user]

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT] | AIMessage:
        if request.system_message is not None:
            new_system_content = [
                *request.system_message.content_blocks,
                {"type": "text", "text": f"\n\n{self.system_prompt}"},
            ]
        else:
            new_system_content = [{"type": "text", "text": self.system_prompt}]
        new_system_message = SystemMessage(
            content=cast("list[str | dict[str, str]]", new_system_content)
        )
        return handler(request.override(system_message=new_system_message))

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[
            [ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]
        ],
    ) -> ModelResponse[ResponseT] | AIMessage:
        if request.system_message is not None:
            new_system_content = [
                *request.system_message.content_blocks,
                {"type": "text", "text": f"\n\n{self.system_prompt}"},
            ]
        else:
            new_system_content = [{"type": "text", "text": self.system_prompt}]
        new_system_message = SystemMessage(
            content=cast("list[str | dict[str, str]]", new_system_content)
        )
        return await handler(request.override(system_message=new_system_message))
