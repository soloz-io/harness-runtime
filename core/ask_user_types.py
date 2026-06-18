from typing import Annotated, Literal, NotRequired

from pydantic import Field
from typing_extensions import TypedDict


class Choice(TypedDict):
    value: Annotated[str, Field(description="The display label for this choice.")]


class Question(TypedDict):
    question: Annotated[str, Field(description="The question text to display.")]

    type: Annotated[
        Literal["text", "multiple_choice"],
        Field(
            description="Question type. 'text' for free-form input, 'multiple_choice' for predefined options."
        ),
    ]

    choices: NotRequired[
        Annotated[
            list[Choice],
            Field(description="Options for multiple_choice questions. An 'Other' option is always appended automatically."),
        ]
    ]

    required: NotRequired[
        Annotated[
            bool,
            Field(description="Whether the user must answer. Defaults to true if omitted."),
        ]
    ]


class AskUserRequest(TypedDict):
    type: Literal["ask_user"]
    questions: list[Question]
    tool_call_id: str


class AskUserAnswered(TypedDict):
    type: Literal["answered"]
    answers: list[str]


class AskUserCancelled(TypedDict):
    type: Literal["cancelled"]


AskUserWidgetResult = AskUserAnswered | AskUserCancelled
