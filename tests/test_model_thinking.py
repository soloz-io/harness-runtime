"""Validate model thinking-mode capabilities for structured-output workflows.

Hardcode TEST_MODEL at the top of this file to target a different model.
Run with:  python3 -m pytest tests/test_model_thinking.py -v -s
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool

# ================================================================== #
#  CONFIG — change this to test a different model                    #
# ================================================================== #
TEST_MODEL: dict[str, Any] = {
    "provider": "openai",
    "model": "deepseek-v4-flash",
    # "provider": "openai",
    # "model": "deepseek-chat",
    # "provider": "anthropic",
    # "model": "claude-sonnet-4-20250514",
}

# ================================================================== #
#  Shared helper — create a model instance                           #
# ================================================================== #


def _make_model(
    *,
    thinking_disabled: bool = False,
    response_format: Any = None,
) -> Any:
    """Create a Chat* model for TEST_MODEL, optionally disabling thinking."""
    from core.model_factory import ModelFactory

    extra: dict[str, Any] = {}
    if thinking_disabled:
        extra["extra_body"] = {"thinking": {"type": "disabled"}}
    return ModelFactory.create_model(
        provider=TEST_MODEL["provider"],
        model_name=TEST_MODEL["model"],
        **extra,
    )


@tool
def _dummy_weather(location: str) -> str:
    """Get weather for a location."""
    return f"Sunny 22°C in {location}"


# ================================================================== #
#  Tests                                                             #
# ================================================================== #


class TestThinkingModeCapabilities:
    """Probe model behaviour around thinking + tool_choice + reasoning_content."""

    def test_thinking_with_tool_call(self) -> None:
        """Verify model with thinking enabled + tool calls succeeds.

        If this fails, the model DOES support thinking + tools.
        If it fails, model rejected the request.
        """
        model = _make_model()
        messages = [
            SystemMessage(content="You are a helpful assistant."),
            HumanMessage(content="What's the weather in Tokyo?"),
        ]
        result = model.invoke(messages, tools=[_dummy_weather])
        print(f"  response: content={result.content!r}")
        print(f"  tool_calls={result.tool_calls!r}")
        rc = result.additional_kwargs.get("reasoning_content")
        print(f"  reasoning_content={'present' if rc else 'absent'} (len={len(rc) if rc else 0})")

    def test_thinking_with_tool_choice(self) -> None:
        """Thinking mode + tool_choice forces a tool call.

        Expected: FAILS for older DeepSeek models (thinking + tool_choice
        incompatible). SUCCEEDS for DeepSeek >= V3.2.
        """
        import langchain_core.messages as lc_messages

        model = _make_model()
        messages = [
            SystemMessage(content="You are a helpful assistant. Use the weather tool."),
            HumanMessage(content="What's the weather in Tokyo?"),
        ]

        # Build a ToolStrategy response_format (simulates what
        # build_tool_strategy does for nodes with response_format).
        try:
            from langchain.agents.structured_output import ToolStrategy
        except ImportError:
            pytest.skip("ToolStrategy not available")

        schema = {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "temperature": {"type": "string"},
            },
            "required": ["city", "temperature"],
        }
        strategy = ToolStrategy(schema=schema)

        try:
            result = model.invoke(messages, tools=[_dummy_weather], response_format=strategy)
            print(f"  SUCCESS — model accepts thinking + tool_choice")
            print(f"  response: {result.content!r}")
            assert any(tc["name"] == "extract_weather" or "extract" in tc["name"] for tc in (result.tool_calls or []))
        except Exception as e:
            err_str = str(e)
            if "tool_choice" in err_str.lower() or "thinking mode" in err_str.lower():
                pytest.skip(
                    f"Model {TEST_MODEL['model']} does not support "
                    f"thinking + tool_choice: {err_str}"
                )
            raise

    def test_reasoning_content_preserved_through_serialization(self) -> None:
        """Verify monkey-patch injects reasoning_content into serialized dict.

        This test validates the fix in core/structured_output._patched_convert_message_to_dict.
        """
        msg = AIMessage(
            content="Final answer",
            additional_kwargs={"reasoning_content": "Chain of thought..."},
            tool_calls=[{"name": "dummy", "args": {}, "id": "call_1", "type": "tool_call"}],
        )

        from langchain_openai.chat_models.base import _convert_message_to_dict

        d = _convert_message_to_dict(msg)
        print(f"  serialized: {json.dumps(d, indent=2, default=str)}")

        assert "reasoning_content" in d, (
            f"reasoning_content missing from serialized dict! "
            f"Monkey-patch not active. Got keys: {list(d.keys())}"
        )
        assert d["reasoning_content"] == "Chain of thought..."

    def test_thinking_disabled_with_tool_choice(self) -> None:
        """Thinking disabled + tool_choice (structured output scenario).

        This is the reviewer/retro node scenario: model must accept
        tool_choice when thinking is disabled.
        """
        try:
            from langchain.agents.structured_output import ToolStrategy
        except ImportError:
            pytest.skip("ToolStrategy not available")

        model = _make_model(thinking_disabled=True)
        messages = [
            SystemMessage(content="You are a reviewer. Output structured feedback."),
            HumanMessage(content="Review this PR: fix button click in Safari"),
        ]

        schema = {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean"},
                "feedback": {"type": "string"},
            },
            "required": ["approved", "feedback"],
        }
        strategy = ToolStrategy(schema=schema)

        result = model.invoke(messages, response_format=strategy)
        print(f"  result: {result.content!r}")
        print(f"  tool_calls={result.tool_calls!r}")
        assert result.tool_calls, "Expected a tool call for structured output"

    def test_chain_thinking_then_thinking_disabled(self) -> None:
        """Chain: thinking-enabled call -> thinking-disabled call with tool_choice.

        This is the full scenario: implementer (thinking) -> verifier (thinking)
        -> reviewer (thinking-disabled + tool_choice). Tests that reasoning_content
        from thinking calls is preserved by the monkey-patch, and the
        thinking-disabled call with tool_choice succeeds.
        """
        try:
            from langchain.agents.structured_output import ToolStrategy
        except ImportError:
            pytest.skip("ToolStrategy not available")

        # --- Turn 1: thinking-enabled call ---
        model_t = _make_model(thinking_disabled=False)
        messages: list[Any] = [
            SystemMessage(content="You are a coding assistant."),
            HumanMessage(content="Write a Python function to add two numbers"),
        ]
        result_t = model_t.invoke(messages, tools=[_dummy_weather])
        messages.append(result_t)
        rc1 = result_t.additional_kwargs.get("reasoning_content")
        print(f"  Turn 1 reasoning_content={'present' if rc1 else 'absent'}")

        # Add a fake tool result to simulate tool execution
        messages.append({
            "role": "tool",
            "tool_call_id": result_t.tool_calls[0]["id"] if result_t.tool_calls else "call_1",
            "content": "Sunny 22°C",
        })

        # --- Turn 2: thinking-enabled call ---
        messages.append(HumanMessage(content="Now convert to Celsius"))
        result_t2 = model_t.invoke(messages, tools=[_dummy_weather])
        messages.append(result_t2)
        rc2 = result_t2.additional_kwargs.get("reasoning_content")
        print(f"  Turn 2 reasoning_content={'present' if rc2 else 'absent'}")

        # Add tool result
        messages.append({
            "role": "tool",
            "tool_call_id": result_t2.tool_calls[0]["id"] if result_t2.tool_calls else "call_2",
            "content": "22°C = 71.6°F",
        })

        # --- Turn 3: thinking-disabled call with tool_choice (reviewer scenario) ---
        model_nt = _make_model(thinking_disabled=True)
        messages.append(HumanMessage(content="Review the final answer for correctness."))

        schema = {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean"},
                "feedback": {"type": "string"},
            },
            "required": ["approved", "feedback"],
        }
        strategy = ToolStrategy(schema=schema)

        result_nt = model_nt.invoke(messages, response_format=strategy)
        print(f"  Turn 3 result: {result_nt.content!r}")
        print(f"  Turn 3 tool_calls={result_nt.tool_calls!r}")
        assert result_nt.tool_calls, "Expected a tool call for structured output"
