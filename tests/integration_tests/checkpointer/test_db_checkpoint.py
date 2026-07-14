"""Business integration tests — DB checkpoint persistence.

Tests the harness-runtime HTTP server as a black box:
  - POST /session/{id}/message to create session + send message
  - GET /event (SSE) to stream frames back
  - Query PostgreSQL directly for checkpoint persistence

Requires:
  DEEPSEEK_API_KEY   — LLM provider API key
  DATABASE_URL       — PostgreSQL connection string

Run with:
  export DEEPSEEK_API_KEY="..."
  export DATABASE_URL="postgresql://..."
  PYTHONPATH=. uv run pytest tests/integration_tests/checkpointer/test_db_checkpoint.py -v
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from dotenv import load_dotenv

from tests.integration_tests.conftest import BASE_URL, sse_server  # noqa: F401
from tests.integration_tests.helpers import count_checkpoints, read_sse_frames

env_path = Path(__file__).parent.parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

import pytest

MOCK_DATA = Path(__file__).parent.parent / "mock" / "simple-bug-fix-invoke-requests.json"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="Mock fixture file not found: tests/integration_tests/mock/simple-bug-fix-invoke-requests.json"
)
def test_initialize_and_single_turn(sse_server: None) -> None:
    """Initialize → message → system/init → result + checkpoints."""
    payloads = json.loads(MOCK_DATA.read_text())
    step = list(payloads["requests"].values())[0]
    user_content = step["input_payload"]["messages"][0]["content"]

    session_id = str(uuid.uuid4())

    httpx.post(
        f"{BASE_URL}/session/{session_id}/message",
        json={
            "message": user_content,
            "agent_definition": step["agent_definition"],
            "input_payload": step["input_payload"],
            "workspace_id": "test-workspace",
        },
        timeout=30.0,
    )

    with httpx.stream(
        "GET",
        f"{BASE_URL}/event?session_id={session_id}",
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(120.0),
    ) as sse_resp:
        frames = read_sse_frames(sse_resp)

    # First non-ping event should be lifecycle started
    first_event = next(f for f in frames if f.get("type") == "event")
    first_method = first_event.get("method")
    assert first_method == "lifecycle", f"Expected lifecycle, got {first_method}"

    assert frames[-1]["type"] == "result"
    assert frames[-1]["session_id"] == session_id

    count_checkpoints(session_id)
