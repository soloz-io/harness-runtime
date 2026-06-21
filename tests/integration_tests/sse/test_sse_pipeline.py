"""Business integration tests — SSE event pipeline.

Tests the harness-runtime HTTP server as a black box (same as SDK usage):
  - HTTP server subprocess (uvicorn via cli.py)
  - Concurrent SSE stream (GET /event) + POST message (POST /session/{id}/message)
  - Parse SSE frames until result
  - Validate frame ordering, session identity, and fan-out

Requires:
  DEEPSEEK_API_KEY   — LLM provider API key (deepseek-v4-flash)
  DATABASE_URL       — PostgreSQL connection string
  redis-server       — available on PATH

Business journey assertions:
  S1: SSE delivers connected, system, assistant, and result frames in order
  S2: Frames contain the correct session_id
  S3: Two concurrent SSE consumers receive the same events
  S4: SSE stream terminates cleanly after result frame

Run with:
  export DEEPSEEK_API_KEY="..."
  export DATABASE_URL="postgresql://..."
  PYTHONPATH=. uv run pytest tests/integration_tests/sse/test_sse_pipeline.py -v
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import httpx
from dotenv import load_dotenv

from tests.integration_tests.conftest import BASE_URL, sse_server  # noqa: F401
from tests.integration_tests.helpers import read_sse_frames

env_path = __import__("pathlib").Path(__file__).parent.parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


# ---------------------------------------------------------------------------
# Agent definition (simple — no HITL gate)
# ---------------------------------------------------------------------------

_MODEL = {"provider": "openai", "model_name": "deepseek-v4-flash"}

AGENT_SIMPLE: dict[str, Any] = {
    "tool_definitions": [],
    "nodes": [
        {
            "id": "orchestrator",
            "type": "orchestrator",
            "config": {
                "name": "sse-test-agent",
                "model": dict(_MODEL),
                "tools": [],
                "system_prompt": "Respond with just the word 'Hello' and nothing else.",
            },
        }
    ],
    "edges": [],
}

_INPUT_PAYLOAD: dict[str, Any] = {
    "messages": [{"role": "user", "content": "Say hello."}],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sse_delivers_frame_types(sse_server: None) -> None:
    """S1: SSE delivers connected, system, assistant, and result frames in order.

    Business outcome: SDK receives all frame types needed to render
    the assistant's response progressively.
    """
    session_id = str(uuid.uuid4())

    # POST first to create session (execution runs in background)
    httpx.post(
        f"{BASE_URL}/session/{session_id}/message",
        json={
            "message": "Say hello.",
            "agent_definition": AGENT_SIMPLE,
            "input_payload": dict(_INPUT_PAYLOAD),
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

    types = [f["type"] for f in frames]

    assert "connected" in types, f"Missing connected frame. Types: {types}"
    assert "system" in types, f"Missing system frame. Types: {types}"
    assert "assistant" in types, f"Missing assistant frame. Types: {types}"
    assert "result" in types, f"Missing result frame. Types: {types}"

    assert types.index("connected") < types.index("system"), (
        f"connected ({types.index('connected')}) should precede "
        f"system ({types.index('system')}). Types: {types}"
    )
    assert types.index("system") < types.index("result"), (
        f"system ({types.index('system')}) should precede "
        f"result ({types.index('result')}). Types: {types}"
    )
    assert types.index("assistant") < types.index("result"), (
        f"assistant ({types.index('assistant')}) should precede "
        f"result ({types.index('result')}). Types: {types}"
    )

    # system frame has correct subtype
    system_frames = [f for f in frames if f.get("type") == "system"]
    assert len(system_frames) >= 1
    assert system_frames[0].get("subtype") == "init", (
        f"system frame subtype is {system_frames[0].get('subtype')}, expected init"
    )

    # result frame indicates success
    result_frame = next(f for f in frames if f.get("type") == "result")
    assert result_frame.get("subtype") in ("success",), (
        f"result subtype is {result_frame.get('subtype')}, expected success"
    )


def test_sse_events_have_correct_session(sse_server: None) -> None:
    """S2: SSE frames carry the requesting session_id.

    Business outcome: SDK can match frames to their session, confirming
    events go to the correct stream rather than a stale or wrong one.
    """
    session_id = str(uuid.uuid4())

    httpx.post(
        f"{BASE_URL}/session/{session_id}/message",
        json={
            "message": "Say hello.",
            "agent_definition": AGENT_SIMPLE,
            "input_payload": dict(_INPUT_PAYLOAD),
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

    for frame in frames:
        if "session_id" in frame:
            assert frame["session_id"] == session_id, (
                f"Frame {frame.get('type')} has wrong session_id: "
                f"{frame['session_id']} != {session_id}"
            )

    # connected frame always carries session_id
    connected = next(f for f in frames if f.get("type") == "connected")
    assert connected["session_id"] == session_id


def test_multi_device_fan_out(sse_server: None) -> None:
    """S3: Two concurrent SSE consumers receive the same frames.

    Business outcome: Desktop and Mobile clients viewing the same
    session see identical frame sequences.
    """
    session_id = str(uuid.uuid4())

    httpx.post(
        f"{BASE_URL}/session/{session_id}/message",
        json={
            "message": "Say hello.",
            "agent_definition": AGENT_SIMPLE,
            "input_payload": dict(_INPUT_PAYLOAD),
        },
        timeout=30.0,
    )

    collected: list[list[dict[str, Any]]] = [[], []]
    exc_info: list[Exception | None] = [None, None]

    def _consume(idx: int) -> None:
        try:
            with httpx.stream(
                "GET",
                f"{BASE_URL}/event?session_id={session_id}",
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(120.0),
            ) as resp:
                collected[idx] = read_sse_frames(resp)
        except Exception as exc:
            exc_info[idx] = exc

    threads = [threading.Thread(target=_consume, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()

    for t in threads:
        t.join(timeout=120.0)

    for i, exc in enumerate(exc_info):
        if exc is not None:
            pytest.fail(f"Consumer {i} failed: {exc}")

    assert len(collected[0]) > 0, "Consumer 0 received no frames"
    assert len(collected[1]) > 0, "Consumer 1 received no frames"

    assert len(collected[0]) == len(collected[1]), (
        f"Frame count mismatch: {len(collected[0])} vs {len(collected[1])}. "
        f"C0 types: {[f.get('type') for f in collected[0]]}. "
        f"C1 types: {[f.get('type') for f in collected[1]]}."
    )

    for i, (a, b) in enumerate(zip(collected[0], collected[1])):
        assert a == b, f"Frame {i} mismatch: {a} != {b}"


def test_sse_stream_terminates_cleanly(sse_server: None) -> None:
    """S4: SSE connection closes after result frame.

    Business outcome: Connections are not orphaned; resources are
    cleaned up after execution completes.
    """
    session_id = str(uuid.uuid4())

    httpx.post(
        f"{BASE_URL}/session/{session_id}/message",
        json={
            "message": "Say hello.",
            "agent_definition": AGENT_SIMPLE,
            "input_payload": dict(_INPUT_PAYLOAD),
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

    # Business outcome: last frame is result
    assert frames[-1]["type"] == "result", (
        f"Last frame should be result, got {frames[-1].get('type')}"
    )
    assert frames[-1].get("is_error") is False, "Result frame indicates error"

    # Stream termination: the result frame was delivered and read_sse_frames
    # returned without timeout, confirming the connection closes cleanly.
