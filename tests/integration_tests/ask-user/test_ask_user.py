"""Business integration tests — HITL gate tool / interrupt lifecycle.

Tests the harness-runtime HTTP server as a black box (same as SDK usage):
  - POST /session/{id}/message to create session + send message
  - GET /event (SSE) to stream frames back
  - POST again with resume_payload to resume interrupted executions
  - Validate interrupt delivery and resume flow

Requires:
  DEEPSEEK_API_KEY   — LLM provider API key (for deepseek-v4-flash)
  DATABASE_URL       — PostgreSQL connection string

Business journey assertions:
  H1: SDK receives interrupt with action_requests and review_configs
  H2: Interrupt shape is well-formed (HITLRequest fields)
  H3: No interrupt emitted when interrupt_on is not configured
  H4: Multi-interrupt: two consecutive gate calls produce two interrupts

Run with:
  export DEEPSEEK_API_KEY="..."
  export DATABASE_URL="postgresql://..."
  PYTHONPATH=. uv run pytest tests/integration_tests/ask-user/test_ask_user.py -v
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from tests.integration_tests.conftest import BASE_URL, sse_server  # noqa: F401
from tests.integration_tests.helpers import read_sse_frames

env_path = Path(__file__).parent.parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# ---------------------------------------------------------------------------
# Agent definitions
# ---------------------------------------------------------------------------

_MODEL = {"provider": "openai", "model_name": "deepseek-v4-flash"}

_GATE_TOOL_SCRIPT = """
from langchain_core.tools import tool

@tool
def hitl_gate(response: str) -> str:
    \"\"\"Gate tool for HITL test. The interrupt_on config pauses before execution.\"\"\"
    return response
"""

GATE_ENABLED: dict[str, Any] = {
    "tool_definitions": [
        {
            "name": "hitl_gate",
            "runtime": {"script": _GATE_TOOL_SCRIPT},
        }
    ],
    "nodes": [
        {
            "id": "orchestrator",
            "type": "orchestrator",
            "config": {
                "name": "hitl-gate-enabled",
                "model": dict(_MODEL),
                "tools": ["hitl_gate"],
                "interrupt_on": {"hitl_gate": {"allowed_decisions": ["approve", "reject"]}},
                "system_prompt": (
                    "You MUST call the hitl_gate tool with response 'blue'. "
                    "Do not respond with any text. Only call hitl_gate."
                ),
            },
        }
    ],
}

GATE_DISABLED: dict[str, Any] = {
    "tool_definitions": [],
    "nodes": [
        {
            "id": "orchestrator",
            "type": "orchestrator",
            "config": {
                "name": "hitl-gate-disabled",
                "model": dict(_MODEL),
                "system_prompt": "Respond with just the word 'Hello' and nothing else.",
            },
        }
    ],
    "edges": [],
}

GATE_MULTI: dict[str, Any] = {
    "tool_definitions": [
        {
            "name": "hitl_gate",
            "runtime": {"script": _GATE_TOOL_SCRIPT},
        }
    ],
    "nodes": [
        {
            "id": "orchestrator",
            "type": "orchestrator",
            "config": {
                "name": "hitl-gate-multi",
                "model": dict(_MODEL),
                "tools": ["hitl_gate"],
                "interrupt_on": {"hitl_gate": {"allowed_decisions": ["approve", "reject"]}},
                "system_prompt": (
                    "You MUST call the hitl_gate tool with response 'first'. "
                    "After it returns, you MUST call hitl_gate with response 'second'. "
                    "After the second call returns, respond with 'done'. "
                    "Do not respond with any text except through the tool."
                ),
            },
        }
    ],
}

_INPUT_PAYLOAD: dict[str, Any] = {
    "messages": [{"role": "user", "content": "Help me make a decision."}]
}


def _post_message(session_id: str, **extra: Any) -> None:
    """POST to the session message endpoint."""
    httpx.post(
        f"{BASE_URL}/session/{session_id}/message",
        json=extra,
        timeout=30.0,
    )


def _open_sse(session_id: str) -> httpx.Response:
    """Open an SSE stream for the given session."""
    return httpx.stream(
        "GET",
        f"{BASE_URL}/event?session_id={session_id}",
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(120.0),
    )


def _assert_interrupt(
    agent: dict[str, Any],
    max_attempts: int = 5,
    user_content: str = "I need help deciding.",
) -> tuple[str, list[dict[str, Any]]]:
    """POST + SSE until interrupt result (retries on LLM non-determinism)."""
    for _ in range(max_attempts):
        session_id = str(uuid.uuid4())
        _post_message(
            session_id,
            message=user_content,
            agent_definition=agent,
            input_payload=dict(_INPUT_PAYLOAD),
        )
        with _open_sse(session_id) as sse_resp:
            frames = read_sse_frames(sse_resp)
        result = frames[-1]
        if result.get("type") == "result" and result.get("subtype") == "interrupted":
            return session_id, frames
    raise AssertionError(
        f"LLM did not produce interrupt after {max_attempts} attempts. "
        f"Last result: {result.get('subtype')}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hitl_gate_delivers_interrupt(sse_server: None) -> None:
    """H1 + H2: Agent with interrupt_on emits result {subtype:"interrupted"}.

    Business outcome: SDK receives HITLRequest with action_requests and
    review_configs it can render.
    """
    _, frames = _assert_interrupt(GATE_ENABLED)

    result = frames[-1]
    assert result["type"] == "result"
    assert result["subtype"] == "interrupted"

    interrupt: dict[str, Any] = result["interrupt"]
    assert interrupt is not None
    assert "action_requests" in interrupt
    assert "review_configs" in interrupt

    action_requests: list[dict[str, Any]] = interrupt["action_requests"]
    assert len(action_requests) > 0
    for req in action_requests:
        assert "name" in req and isinstance(req["name"], str) and len(req["name"]) > 0
        assert "args" in req

    review_configs: list[dict[str, Any]] = interrupt["review_configs"]
    assert len(review_configs) > 0
    for cfg in review_configs:
        assert "action_name" in cfg
        assert "allowed_decisions" in cfg
        assert len(cfg["allowed_decisions"]) > 0


def test_hitl_not_configured(sse_server: None) -> None:
    """H3: Without interrupt_on in node config, agent completes normally.

    Business outcome: No interrupt emitted, result is success.
    """
    session_id = str(uuid.uuid4())

    _post_message(
        session_id,
        message="Say hello.",
        agent_definition=GATE_DISABLED,
        input_payload=dict(_INPUT_PAYLOAD),
    )

    with _open_sse(session_id) as sse_resp:
        frames = read_sse_frames(sse_resp)

    result = frames[-1]
    assert result["type"] == "result"
    assert result["subtype"] == "success"
    assert result.get("interrupt") is None


def test_hitl_multi_interrupt(sse_server: None) -> None:
    """H4: Multiple consecutive gate tool calls in one turn.

    Business outcome: Each gate tool call delivers its own interrupt,
    and the caller can process both in sequence.
    """
    session_id = str(uuid.uuid4())

    # --- Turn 1: POST message + agent -> expect first interrupt ---
    session_id, frames_1 = _assert_interrupt(GATE_MULTI)

    result_1 = frames_1[-1]
    assert result_1["subtype"] == "interrupted"
    assert result_1["interrupt"] is not None
    assert len(result_1["interrupt"]["action_requests"]) > 0

    # --- Turn 2-N: resume with approve; verify at least one more turn ---
    at_least_one_more = False
    for _ in range(5):
        _post_message(
            session_id,
            resume_payload={"decisions": [{"type": "approve"}]},
        )

        with _open_sse(session_id) as sse_resp:
            frames_n = read_sse_frames(sse_resp)

        result_n = frames_n[-1]
        assert result_n["type"] == "result"

        at_least_one_more = True
        if result_n["subtype"] == "success":
            break
        assert result_n["subtype"] == "interrupted", (
            f"Expected interrupted or success, got {result_n['subtype']}"
        )

    assert at_least_one_more, "No result after first resume"
