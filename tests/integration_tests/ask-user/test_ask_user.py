"""Business integration tests — ask_user / interrupt lifecycle.

Tests the harness-runtime CLI as a black box (same as SDK usage):
  - Single subprocess per test (function-scoped fixture)
  - Send control_request {initialize} + user {message} via stdin
  - Read LiteLLM frames from stdout
  - Validate interrupt delivery and resume flow

Requires:
  DEEPSEEK_API_KEY   — LLM provider API key (for deepseek-v4-flash)
  DATABASE_URL       — PostgreSQL connection string

Business journey assertions (per ADR-004):
  B1: SDK receives interrupt with questions
  B2: SDK can render questions in the UI
  B3: No interrupt emitted when ask_user is not configured
  B4: Multiple consecutive ask_user calls each deliver their own interrupt

Run with:
  export DEEPSEEK_API_KEY="..."
  export DATABASE_URL="postgresql://waypoint:waypoint@localhost:5433/waypoint_test"
  cd tests && docker compose up -d --wait
  python3 -m pytest tests/integration_tests/ask-user/test_ask_user.py -v
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

env_path = Path(__file__).parent.parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

from tests.integration_tests.helpers import (
    read_frame,
    read_turn,
    save_artifacts,
    send,
)

# ---------------------------------------------------------------------------
# Agent definitions — inline test data, not business logic (ADR-004 §5)
# ---------------------------------------------------------------------------

_MODEL = {"provider": "openai", "model_name": "deepseek-v4-flash"}

AGENT_BASE: dict[str, Any] = {
    "tool_definitions": [],
    "nodes": [
        {
            "id": "orchestrator",
            "type": "orchestrator",
            "config": {
                "name": "ask-user-test",
                "model": dict(_MODEL),
                "tools": [],
            },
        }
    ],
    "edges": [],
}

ASK_USER_ENABLED: dict[str, Any] = {
    **AGENT_BASE,
    "nodes": [
        {
            **AGENT_BASE["nodes"][0],
            "config": {
                **AGENT_BASE["nodes"][0]["config"],
                "allow_ask_user": True,
                "system_prompt": (
                    "You MUST call the ask_user tool with exactly one question: "
                    "'What is your favorite color?'. "
                    "Do not respond with any text. Only call the ask_user tool."
                ),
            },
        }
    ],
}

ASK_USER_DISABLED: dict[str, Any] = {
    **AGENT_BASE,
    "nodes": [
        {
            **AGENT_BASE["nodes"][0],
            "config": {
                **AGENT_BASE["nodes"][0]["config"],
                "system_prompt": "Respond with just the word 'Hello' and nothing else.",
            },
        }
    ],
}

ASK_USER_MULTI: dict[str, Any] = {
    **AGENT_BASE,
    "nodes": [
        {
            **AGENT_BASE["nodes"][0],
            "config": {
                **AGENT_BASE["nodes"][0]["config"],
                "allow_ask_user": True,
                "system_prompt": (
                    "CRITICAL: You must call the ask_user tool now with "
                    "one question 'what is 2+2?'. After you get the answer, "
                    "call ask_user again with 'what is 3+3?'. "
                    "Only call ask_user, say nothing else."
                ),
            },
        }
    ],
}

_INPUT_PAYLOAD: dict[str, Any] = {
    "messages": [{"role": "user", "content": "Help me make a decision."}]
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ask_user_delivers_interrupt(
    harness: subprocess.Popen[bytes], artifact_dir: Path
) -> None:
    """B1 + B2: Agent with ask_user enabled emits result {subtype:"interrupted"}.

    Business outcome: SDK receives interrupt with questions it can render.
    """
    send(harness, {
        "type": "control_request",
        "request_id": "req_1",
        "request": {
            "subtype": "initialize",
            "agent_definition": ASK_USER_ENABLED,
            "input_payload": dict(_INPUT_PAYLOAD),
        },
    })
    init = read_frame(harness)
    assert init["type"] == "control_response"
    assert init["response"]["subtype"] == "success"
    session_id: str = init["response"]["session_id"]
    assert len(session_id) > 0

    send(harness, {
        "type": "user",
        "message": {"role": "user", "content": "I need help deciding."},
        "session_id": None,
        "parent_tool_use_id": None,
    })
    frames = read_turn(harness)
    save_artifacts(artifact_dir, frames)

    result = frames[-1]
    assert result["type"] == "result"
    assert result["subtype"] == "interrupted"

    interrupt: dict[str, Any] = result["interrupt"]
    assert interrupt is not None
    assert interrupt["type"] == "ask_user"

    questions: list[dict[str, Any]] = interrupt["questions"]
    assert len(questions) > 0

    tool_call_id: str = interrupt["tool_call_id"]
    assert isinstance(tool_call_id, str) and len(tool_call_id) > 0

    for q in questions:
        assert "question" in q and isinstance(q["question"], str) and len(q["question"]) > 0
        assert q["type"] in ("text", "multiple_choice")
        if q["type"] == "multiple_choice":
            assert "choices" in q and isinstance(q["choices"], list) and len(q["choices"]) > 0


def test_ask_user_not_configured(
    harness: subprocess.Popen[bytes], artifact_dir: Path
) -> None:
    """B3: Without ask_user in node config, agent completes normally.

    Business outcome: No interrupt emitted, result is success.
    """
    send(harness, {
        "type": "control_request",
        "request_id": "req_1",
        "request": {
            "subtype": "initialize",
            "agent_definition": ASK_USER_DISABLED,
            "input_payload": dict(_INPUT_PAYLOAD),
        },
    })
    init = read_frame(harness)
    assert init["type"] == "control_response"
    assert init["response"]["subtype"] == "success"

    send(harness, {
        "type": "user",
        "message": {"role": "user", "content": "Say hello."},
        "session_id": None,
        "parent_tool_use_id": None,
    })
    frames = read_turn(harness)
    save_artifacts(artifact_dir, frames)

    result = frames[-1]
    assert result["type"] == "result"
    assert result["subtype"] == "success"
    assert result.get("interrupt") is None


def test_ask_user_multi_interrupt(
    harness: subprocess.Popen[bytes], artifact_dir: Path
) -> None:
    """B4: Multiple consecutive ask_user calls in one turn.

    Business outcome: Each ask_user call delivers its own interrupt,
    and the user can answer both in sequence across multiple turns.
    """
    # --- Turn 1: initialize ---
    send(harness, {
        "type": "control_request",
        "request_id": "req_1",
        "request": {
            "subtype": "initialize",
            "agent_definition": ASK_USER_MULTI,
            "input_payload": dict(_INPUT_PAYLOAD),
        },
    })
    init = read_frame(harness)
    assert init["type"] == "control_response"
    assert init["response"]["subtype"] == "success"
    session_id: str = init["response"]["session_id"]
    assert len(session_id) > 0

    # --- Turn 2: user message → expect first interrupt ---
    send(harness, {
        "type": "user",
        "message": {"role": "user", "content": "I need help deciding."},
        "session_id": None,
        "parent_tool_use_id": None,
    })
    frames_1 = read_turn(harness)
    result_1 = frames_1[-1]
    assert result_1["type"] == "result"
    assert result_1["subtype"] == "interrupted"
    assert result_1["interrupt"] is not None
    assert len(result_1["interrupt"]["questions"]) > 0

    # --- Turn 3: resume with answer to first question → expect second interrupt ---
    send(harness, {
        "type": "control_request",
        "request_id": "req_2",
        "request": {
            "subtype": "initialize",
            "session_id": session_id,
            "agent_definition": ASK_USER_MULTI,
            "input_payload": dict(_INPUT_PAYLOAD),
            "resume_payload": {"status": "answered", "answers": ["4"]},
        },
    })
    frames_2 = read_turn(harness)
    result_2 = frames_2[-1]
    assert result_2["type"] == "result"
    assert result_2["subtype"] == "interrupted", (
        f"Expected second interrupt, got {result_2['subtype']}. "
        f"Artifacts: {artifact_dir / 'frames.json'}"
    )
    assert result_2["interrupt"] is not None
    assert len(result_2["interrupt"]["questions"]) > 0

    # Drain the control_response that comes after the result
    ctrl_2 = read_frame(harness)
    assert ctrl_2["type"] == "control_response"
    assert ctrl_2["response"]["subtype"] == "success"

    # --- Turn 4: resume with answer to second question → expect success ---
    send(harness, {
        "type": "control_request",
        "request_id": "req_3",
        "request": {
            "subtype": "initialize",
            "session_id": session_id,
            "agent_definition": ASK_USER_MULTI,
            "input_payload": dict(_INPUT_PAYLOAD),
            "resume_payload": {"status": "answered", "answers": ["9"]},
        },
    })
    frames_3 = read_turn(harness)
    result_3 = frames_3[-1]
    assert result_3["type"] == "result"
    assert result_3["subtype"] == "success", (
        f"Expected success after second resume, got {result_3['subtype']}. "
        f"Artifacts: {artifact_dir / 'frames.json'}"
    )

    # Save all frames for debugging
    all_frames = frames_1 + frames_2 + frames_3
    save_artifacts(artifact_dir, all_frames)
