"""Business integration tests — HITL gate tool / interrupt lifecycle.

Tests the harness-runtime CLI as a black box (same as SDK usage):
  - Single subprocess per test (function-scoped fixture)
  - Send control_request {initialize} + user {message} via stdin
  - Read LiteLLM frames from stdout
  - Validate interrupt delivery and resume flow via HumanInTheLoopMiddleware

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
# Agent definitions — inline test data
# ---------------------------------------------------------------------------

_MODEL = {"provider": "openai", "model_name": "deepseek-v4-flash"}

_GATE_TOOL_SCRIPT = """
from langchain_core.tools import tool

@tool
def hitl_gate(response: str) -> str:
    \"\"\"Gate tool for HITL test. The interrupt_on config pauses before execution.\"\"\"
    return response
"""

AGENT_BASE: dict[str, Any] = {
    "tool_definitions": [],
    "nodes": [
        {
            "id": "orchestrator",
            "type": "orchestrator",
            "config": {
                "name": "hitl-test",
                "model": dict(_MODEL),
                "tools": [],
            },
        }
    ],
    "edges": [],
}

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
                "interrupt_on": {
                    "hitl_gate": {"allowed_decisions": ["approve", "reject"]}
                },
                "system_prompt": (
                    "You MUST call the hitl_gate tool with response 'blue'. "
                    "Do not respond with any text. Only call hitl_gate."
                ),
            },
        }
    ],
}

GATE_DISABLED: dict[str, Any] = {
    **AGENT_BASE,
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
                "interrupt_on": {
                    "hitl_gate": {"allowed_decisions": ["approve", "reject"]}
                },
                "system_prompt": (
                    "CRITICAL: You must call hitl_gate now with response 'first'. "
                    "After you get the result, call hitl_gate again with response 'second'. "
                    "Only call hitl_gate, say nothing else."
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


def test_hitl_gate_delivers_interrupt(
    harness: subprocess.Popen[bytes], artifact_dir: Path
) -> None:
    """H1 + H2: Agent with interrupt_on emits result {subtype:"interrupted"}.

    Business outcome: SDK receives HITLRequest with action_requests and
    review_configs it can render.
    """
    send(harness, {
        "type": "control_request",
        "request_id": "req_1",
        "request": {
            "subtype": "initialize",
            "agent_definition": GATE_ENABLED,
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


def test_hitl_not_configured(
    harness: subprocess.Popen[bytes], artifact_dir: Path
) -> None:
    """H3: Without interrupt_on in node config, agent completes normally.

    Business outcome: No interrupt emitted, result is success.
    """
    send(harness, {
        "type": "control_request",
        "request_id": "req_1",
        "request": {
            "subtype": "initialize",
            "agent_definition": GATE_DISABLED,
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


def test_hitl_multi_interrupt(
    harness: subprocess.Popen[bytes], artifact_dir: Path
) -> None:
    """H4: Multiple consecutive gate tool calls in one turn.

    Business outcome: Each gate tool call delivers its own interrupt,
    and the caller can process both in sequence.
    """
    # --- Turn 1: initialize ---
    send(harness, {
        "type": "control_request",
        "request_id": "req_1",
        "request": {
            "subtype": "initialize",
            "agent_definition": GATE_MULTI,
            "input_payload": dict(_INPUT_PAYLOAD),
        },
    })
    init = read_frame(harness)
    assert init["type"] == "control_response"
    assert init["response"]["subtype"] == "success"
    session_id: str = init["response"]["session_id"]
    assert len(session_id) > 0

    # --- Turn 2: user message -> expect first interrupt ---
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
    assert len(result_1["interrupt"]["action_requests"]) > 0

    # --- Turn 3: resume with approve -> expect second interrupt ---
    send(harness, {
        "type": "control_request",
        "request_id": "req_2",
        "request": {
            "subtype": "initialize",
            "session_id": session_id,
            "agent_definition": GATE_MULTI,
            "input_payload": dict(_INPUT_PAYLOAD),
            "resume_payload": {"decisions": [{"type": "approve"}]},
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
    assert len(result_2["interrupt"]["action_requests"]) > 0

    # Drain the control_response that comes after the result
    ctrl_2 = read_frame(harness)
    assert ctrl_2["type"] == "control_response"
    assert ctrl_2["response"]["subtype"] == "success"

    # --- Turn 4: resume with approve -> expect success ---
    send(harness, {
        "type": "control_request",
        "request_id": "req_3",
        "request": {
            "subtype": "initialize",
            "session_id": session_id,
            "agent_definition": GATE_MULTI,
            "input_payload": dict(_INPUT_PAYLOAD),
            "resume_payload": {"decisions": [{"type": "approve"}]},
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
