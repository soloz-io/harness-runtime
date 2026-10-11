"""An unparseable tool call goes back to the model instead of ending the turn."""

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from core.middleware.invalid_tool_calls import MAX_RETRIES, InvalidToolCallRetryMiddleware


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):  # the fake has no tool binding; the agent needs one
        return self


@tool
def ask_user(question: str) -> str:
    """Ask the user."""
    return "answered"


def broken(call_id: str = "call_1") -> AIMessage:
    """A fresh response whose only tool call has unparseable arguments (a stray closing brace)."""
    return AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "id": call_id,
                "name": "ask_user",
                "args": '{"question": "Which brand?"}}',
                "error": "Failed to parse tool call arguments as JSON",
                "type": "invalid_tool_call",
            }
        ],
    )


def run(responses):
    agent = create_agent(
        model=FakeModel(messages=iter(responses)),
        tools=[ask_user],
        middleware=[InvalidToolCallRetryMiddleware()],
    )
    return agent.invoke({"messages": [("user", "hi")]})["messages"]


def test_the_model_is_told_and_calls_again():
    fixed = AIMessage(
        content="",
        tool_calls=[{"id": "call_2", "name": "ask_user", "args": {"question": "Which brand?"}}],
    )
    done = AIMessage(content="Thanks.")
    messages = run([broken(), fixed, done])
    errors = [m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "call_1"]
    assert (
        len(errors) == 1
        and "not valid JSON" in errors[0].content
        and "Failed to parse" in errors[0].content
    )
    # The retried call ran, and the turn went on to the model's answer.
    assert any(
        isinstance(m, ToolMessage) and m.tool_call_id == "call_2" and m.content == "answered"
        for m in messages
    )
    assert messages[-1].content == "Thanks."


def test_it_gives_up_after_the_retries():
    messages = run([broken(f"c{i}") for i in range(MAX_RETRIES + 2)])
    attempts = [m for m in messages if isinstance(m, AIMessage) and m.invalid_tool_calls]
    assert len(attempts) == MAX_RETRIES + 1


def test_a_valid_response_is_untouched():
    messages = run([AIMessage(content="All done.")])
    assert [m.content for m in messages if isinstance(m, AIMessage)] == ["All done."]
