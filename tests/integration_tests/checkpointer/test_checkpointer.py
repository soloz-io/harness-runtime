"""Business integration tests — checkpoint persistence and resume lifecycle.

Tests the harness-runtime CLI as a black box (same as SDK usage):
  - Single subprocess per test (function-scoped fixture)
  - Send control_request {initialize} + user {message} via stdin
  - Read LiteLLM frames from stdout
  - Query PostgreSQL directly for checkpoint persistence

Requires:
  DEEPSEEK_API_KEY   — LLM provider API key
  DATABASE_URL       — PostgreSQL connection string

Business journey assertions (per ADR-002):
  E1: Interrupt → checkpoint is saved with the HumanInTheLoopMiddleware
      interrupt value
  E2: Resume → checkpoint is restored, Command(resume=...) returns
      the resume_payload to the interrupt() call site
  E3: Multiple turns with interrupts → each turn checkpoints and
      resumes independently

Run with:
  export DEEPSEEK_API_KEY="..."
  export DATABASE_URL="postgresql://waypoint:waypoint@localhost:5433/waypoint_test"
  cd tests && docker compose up -d --wait
  python3 -m pytest tests/integration_tests/checkpointer/test_checkpointer.py -v
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
    count_checkpoints,
    get_checkpoint_ids,
    get_checkpoint_metadata,
    initialize_and_assert_interrupt,
    read_frame,
    read_frame_fast,
    read_turn,
    read_turn_fast,
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
                "interrupt_on": {
                    "checkpoint_gate": {"allowed_decisions": ["approve"]}
                },
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e1_checkpoint_saved_on_interrupt(
    harness: subprocess.Popen[bytes], artifact_dir: Path
) -> None:
    """E1: Interrupt → checkpoint is saved.

    Business outcome: At least one checkpoint exists in PostgreSQL for the
    session. Metadata confirms state was persisted (step > 0, messages > 0).
    """
    session_id, frames = initialize_and_assert_interrupt(
        harness, AGENT_GATE, artifact_dir
    )
    save_artifacts(artifact_dir, frames)

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
    assert msg_counters[1] >= 1, (
        f"Expected at least 1 total message version, got {msg_counters[1]}"
    )


def test_e2_resume_returns_payload(
    harness: subprocess.Popen[bytes], artifact_dir: Path
) -> None:
    """E2: Resume → checkpoint is restored, Command(resume=...) returns
    the resume_payload to the interrupt() call site.

    Business outcome: After sending resume, the result subtype is 'success',
    and the agent consumed the resume value.
    """
    session_id, frames = initialize_and_assert_interrupt(
        harness, AGENT_GATE, artifact_dir
    )

    before_count = count_checkpoints(session_id)
    assert before_count >= 1

    send(harness, {
        "type": "control_request",
        "request_id": "req_resume",
        "request": {
            "subtype": "initialize",
            "session_id": session_id,
            "agent_definition": AGENT_GATE,
            "input_payload": dict(_INPUT_PAYLOAD),
            "resume_payload": {"decisions": [{"type": "approve"}]},
        },
    })
    resume_frames = read_turn_fast(harness)

    # drain trailing control_response (may or may not be present)
    try:
        ctrl = read_frame_fast(harness, timeout_sec=5.0)
        assert ctrl["type"] == "control_response"
        assert ctrl["response"]["subtype"] == "success"
    except TimeoutError:
        pass

    all_frames = frames + resume_frames
    save_artifacts(artifact_dir, all_frames)

    resume_result = resume_frames[-1]
    assert resume_result["type"] == "result"
    assert resume_result["subtype"] == "success", (
        f"Expected success after resume, got {resume_result['subtype']}. "
        f"Resume frames: {json.dumps(resume_frames, indent=2, default=str)}"
    )

    after_count = count_checkpoints(session_id)
    assert after_count > before_count, (
        f"Expected new checkpoint after resume (was {before_count}, now {after_count})"
    )


def test_e3_multi_turn_checkpoints(
    harness: subprocess.Popen[bytes], artifact_dir: Path
) -> None:
    """E3: Multiple turns with interrupts → each turn checkpoints
    and resumes independently via HumanInTheLoopMiddleware.

    Business outcome: Checkpoint count increases monotonically with each
    turn. Metadata step values confirm progress.
    """
    session_id, turn1 = initialize_and_assert_interrupt(
        harness, AGENT_GATE, artifact_dir
    )

    ids_1 = get_checkpoint_ids(session_id)
    count_1 = len(ids_1)
    assert count_1 >= 1

    # --- Turn 2: resume with approve → expect success ---
    send(harness, {
        "type": "control_request",
        "request_id": "req_resume1",
        "request": {
            "subtype": "initialize",
            "session_id": session_id,
            "agent_definition": AGENT_GATE,
            "input_payload": dict(_INPUT_PAYLOAD),
            "resume_payload": {"decisions": [{"type": "approve"}]},
        },
    })
    turn2 = read_turn(harness)
    r2 = turn2[-1]
    assert r2["type"] == "result"

    read_frame(harness)  # drain control_response

    ids_2 = get_checkpoint_ids(session_id)
    count_2 = len(ids_2)
    assert count_2 > count_1, (
        f"Expected more checkpoints after turn 2 (was {count_1}, now {count_2})"
    )

    all_frames = turn1 + turn2
    save_artifacts(artifact_dir, all_frames)

    assert r2["subtype"] == "success", (
        f"Expected success after single resume, got {r2['subtype']}"
    )

    metadata = get_checkpoint_metadata(session_id)
    assert metadata is not None
    assert metadata.get("step", 0) >= count_2, (
        f"Expected step >= {count_2}, got step={metadata.get('step', 0)}"
    )
