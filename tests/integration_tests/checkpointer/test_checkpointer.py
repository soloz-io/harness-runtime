"""Business integration tests — checkpoint persistence and resume lifecycle.

Tests the harness-runtime HTTP server as a black box:
  - POST /session/{id}/message to create session + send message
  - GET /event (SSE) to stream frames back
  - Query PostgreSQL directly for checkpoint persistence

Requires:
  DEEPSEEK_API_KEY   — LLM provider API key
  DATABASE_URL       — PostgreSQL connection string

Business journey assertions (per ADR-002):
  E1: Interrupt → checkpoint is saved in PostgreSQL
  E2: Resume → success with increased checkpoint count
  E3: Multiple turns → checkpoints increase monotonically

Run with:
  export DEEPSEEK_API_KEY="..."
  export DATABASE_URL="postgresql://..."
  PYTHONPATH=. uv run pytest tests/integration_tests/checkpointer/test_checkpointer.py -v
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from tests.integration_tests.conftest import BASE_URL, sse_server  # noqa: F401
from tests.integration_tests.helpers import (
    count_checkpoints,
    get_checkpoint_ids,
    get_checkpoint_metadata,
    read_sse_frames,
)

env_path = Path(__file__).parent.parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# ---------------------------------------------------------------------------
# Agent definitions
# ---------------------------------------------------------------------------

_MODEL = {"provider": "openai", "model_name": "deepseek-v4-flash"}

_GATE_TOOL_SCRIPT = """
from langchain_core.tools import tool

@tool
def checkpoint_gate(response: str) -> str:
    \"\"\"Signal checkpoint gate. The interrupt_on config pauses before execution.\"\"\"
    return response
"""

AGENT_GATE: dict[str, Any] = {
    "tool_definitions": [
        {
            "name": "checkpoint_gate",
            "runtime": {"script": _GATE_TOOL_SCRIPT},
        }
    ],
    "nodes": [
        {
            "id": "orchestrator",
            "type": "orchestrator",
            "config": {
                "name": "checkpointer-gate",
                "model": dict(_MODEL),
                "tools": ["checkpoint_gate"],
                "interrupt_on": {"checkpoint_gate": {"allowed_decisions": ["approve"]}},
                "system_prompt": (
                    "Call checkpoint_gate with response 'blue' and then stop. "
                    "Do NOT call checkpoint_gate more than once."
                ),
            },
        }
    ],
    "edges": [],
}

_INPUT_PAYLOAD: dict[str, Any] = {
    "messages": [{"role": "user", "content": "Help me make a decision."}]
}


def _post_message(session_id: str, **extra: Any) -> None:
    payload = dict(extra)
    payload.setdefault("workspace_id", "test-workspace")
    httpx.post(
        f"{BASE_URL}/session/{session_id}/message",
        json=payload,
        timeout=30.0,
    )


def _open_sse(session_id: str) -> httpx.Response:
    return httpx.stream(
        "GET",
        f"{BASE_URL}/event?session_id={session_id}",
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(120.0),
    )


def _assert_interrupt(
    agent: dict[str, Any],
    max_attempts: int = 5,
    user_content: str = "Help me make a decision.",
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


def test_e1_checkpoint_saved_on_interrupt(sse_server: None) -> None:
    """E1: Interrupt → checkpoint is saved.

    Business outcome: At least one checkpoint exists in PostgreSQL for the
    session. Metadata confirms state was persisted (step > 0, messages > 0).
    """
    session_id, frames = _assert_interrupt(AGENT_GATE)

    count = count_checkpoints(session_id)
    assert count >= 1, f"Expected at least 1 checkpoint, got {count}"

    metadata = get_checkpoint_metadata(session_id)
    assert metadata is not None, "No metadata found for session"
    assert metadata.get("step", 0) >= 1, (
        f"Expected step >= 1 in checkpoint metadata, got step={metadata.get('step', 0)}"
    )
    counters = metadata.get("counters_since_delta_snapshot", {})
    msg_counters = counters.get("messages", [0, 0])
    assert msg_counters[0] >= 0, "Invalid message counter"
    assert msg_counters[1] >= 1, f"Expected at least 1 total message version, got {msg_counters[1]}"


def test_e2_resume_returns_payload(sse_server: None) -> None:
    """E2: Resume → checkpoint is restored, result back.

    Business outcome: After sending resume, at least one more checkpoint
    is saved (the model may continue with another turn, which is fine).
    """
    session_id, frames = _assert_interrupt(AGENT_GATE)

    before_count = count_checkpoints(session_id)
    assert before_count >= 1

    for _ in range(3):
        _post_message(
            session_id,
            resume_payload={"decisions": [{"type": "approve"}]},
        )

        with _open_sse(session_id) as sse_resp:
            resume_frames = read_sse_frames(sse_resp)

        resume_result = resume_frames[-1]
        assert resume_result["type"] == "result"
        if resume_result["subtype"] == "success":
            break
        assert resume_result["subtype"] == "interrupted", (
            f"Expected interrupted or success, got {resume_result['subtype']}"
        )

    after_count = count_checkpoints(session_id)
    assert after_count > before_count, (
        f"Expected more checkpoints after resume (was {before_count}, now {after_count})"
    )


def test_e3_multi_turn_checkpoints(sse_server: None) -> None:
    """E3: Multiple turns → checkpoints increase monotonically.

    Business outcome: Checkpoint count increases after resume.
    Metadata step values confirm progress.
    """
    session_id, turn1 = _assert_interrupt(AGENT_GATE)

    ids_1 = get_checkpoint_ids(session_id)
    count_1 = len(ids_1)
    assert count_1 >= 1

    # Resume; LLM may produce additional interrupts (non-deterministic).
    r2 = None
    for _ in range(5):
        _post_message(
            session_id,
            resume_payload={"decisions": [{"type": "approve"}]},
        )

        with _open_sse(session_id) as sse_resp:
            turn2 = read_sse_frames(sse_resp)

        r2 = turn2[-1]
        assert r2["type"] == "result"
        if r2["subtype"] == "success":
            break
        assert r2["subtype"] == "interrupted", (
            f"Expected interrupted or success, got {r2['subtype']}"
        )

    ids_2 = get_checkpoint_ids(session_id)
    count_2 = len(ids_2)
    assert count_2 > count_1, (
        f"Expected more checkpoints after turn 2 (was {count_1}, now {count_2})"
    )

    metadata = get_checkpoint_metadata(session_id)
    assert metadata is not None
    assert metadata.get("step", 0) >= 1, f"Expected step >= 1, got step={metadata.get('step', 0)}"
