"""Business integration tests — real CLI, real DB, real LLM.

Tests the harness-runtime CLI as a black box (same as SDK usage):
  - Single subprocess per test (function-scoped fixture)
  - Send control_request {initialize} + user {message} via stdin
  - Read LiteLLM frames from stdout
  - Validate frame sequence and shape
  - Query PostgreSQL directly for checkpoint persistence

Requires:
  OPENAI_API_KEY or DEEPSEEK_API_KEY — LLM provider API key
  DATABASE_URL                        — PostgreSQL connection string

Run with:
  cd tests && docker compose up -d --wait
  python3 -m pytest tests/test_db_checkpoint.py -v --timeout 180
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

MOCK_DATA = Path(__file__).parent.parent / "mock" / "simple-bug-fix-invoke-requests.json"

# Fail hard if requirements not met
_MISSING: list[str] = []
if (
    not os.environ.get("OPENAI_API_KEY")
    and not os.environ.get("DEEPSEEK_API_KEY")
    and not os.environ.get("ANTHROPIC_API_KEY")
):
    _MISSING.append("OPENAI_API_KEY, DEEPSEEK_API_KEY, or ANTHROPIC_API_KEY")
if not os.environ.get("DATABASE_URL"):
    _MISSING.append("DATABASE_URL (PostgreSQL connection string)")
if _MISSING:
    pytest.fail("Required environment variables:\n  " + "\n  ".join(_MISSING))

from tests.integration_tests.helpers import (
    read_frame,
    read_turn,
    save_artifacts,
    send,
    count_checkpoints,
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_initialize_and_single_turn(harness: subprocess.Popen[bytes], artifact_dir: Path) -> None:
    """Initialize → user → system/init → result + checkpoints."""
    payloads = json.loads(MOCK_DATA.read_text())
    step = list(payloads["requests"].values())[0]
    user_content = step["input_payload"]["messages"][0]["content"]

    send(
        harness,
        {
            "type": "control_request",
            "request_id": "req_1",
            "request": {
                "subtype": "initialize",
                "agent_definition": step["agent_definition"],
                "input_payload": step["input_payload"],
            },
        },
    )
    init_resp = read_frame(harness)
    assert init_resp["type"] == "control_response"
    assert init_resp["response"]["subtype"] == "success"
    session_id = init_resp["response"]["session_id"]
    assert len(session_id) > 0

    send(
        harness,
        {
            "type": "user",
            "message": {"role": "user", "content": user_content},
            "session_id": None,
            "parent_tool_use_id": None,
        },
    )
    frames = read_turn(harness)
    save_artifacts(artifact_dir, frames)

    assert frames[0]["type"] == "system"
    assert frames[0]["subtype"] == "init"
    assert frames[0]["session_id"] == session_id

    assert frames[-1]["type"] == "result"
    assert frames[-1]["session_id"] == session_id

    count_checkpoints(session_id)


def test_user_without_initialize_returns_error(
    harness: subprocess.Popen[bytes], artifact_dir: Path
) -> None:
    """User message without initialize → error result frame."""
    send(
        harness,
        {
            "type": "user",
            "message": {"role": "user", "content": "hello"},
            "session_id": None,
            "parent_tool_use_id": None,
        },
    )
    frames = read_turn(harness)
    save_artifacts(artifact_dir, frames)
    assert frames[-1]["type"] == "result"
    assert frames[-1]["is_error"] is True
