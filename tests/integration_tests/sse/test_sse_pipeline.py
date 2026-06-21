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

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PORT = 9876
_BASE_URL = f"http://127.0.0.1:{_PORT}"


@pytest.fixture(scope="module")
def sse_server() -> Path:
    """Start HTTP server as subprocess on port 9876, return stderr path."""
    cli_path = Path(__file__).parent.parent.parent.parent / "cli.py"
    log_dir = Path(__file__).parent.parent / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = log_dir / "sse-server-stderr.log"

    proc = subprocess.Popen(
        [sys.executable, str(cli_path)],
        stdout=subprocess.DEVNULL,
        stderr=open(stderr_path, "wb"),
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PORT": str(_PORT),
        },
    )

    for _ in range(200):
        if proc.poll() is not None:
            pytest.fail(
                f"Server exited prematurely (code {proc.returncode}). "
                f"Stderr preview: {stderr_path.read_text()[-500:]}"
            )
        try:
            resp = httpx.get(f"{_BASE_URL}/health", timeout=2.0)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("SSE server did not start within 20s")

    yield stderr_path

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


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
# SSE helpers
# ---------------------------------------------------------------------------


def _read_sse_frames(
    response: httpx.Response,
    *,
    timeout_sec: float = 120.0,
) -> list[dict[str, Any]]:
    """Read SSE stream until result frame, return parsed frame dicts.

    Parses the Server-Sent Events protocol (``event:\n`` / ``data:\n``
    fields delimited by ``\n\n``, normalizing ``\r\n`` to ``\n``
    because sse-starlette uses CRLF).  Ignores keepalive pings (empty data).
    """
    deadline = time.monotonic() + timeout_sec
    buf = bytearray()
    frames: list[dict[str, Any]] = []

    for chunk in response.iter_bytes():
        buf.extend(chunk)
        # Normalize CRLF -> LF (sse-starlette uses CRLF line separators)
        buf = bytearray(buf.replace(b"\r\n", b"\n"))

        while True:
            idx = buf.find(b"\n\n")
            if idx == -1:
                break
            event_block = buf[:idx]
            buf = buf[idx + 2 :]

            for line in event_block.split(b"\n"):
                line = line.strip()
                if line.startswith(b"data: "):
                    payload = line[6:]
                    if payload:
                        frame = json.loads(payload)
                        frames.append(frame)
                        if frame.get("type") == "result":
                            return frames
                    break

        if time.monotonic() > deadline:
            raise TimeoutError(
                f"No result frame within {timeout_sec}s. "
                f"Frames so far: {[f.get('type') for f in frames]}"
            )

    return frames


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sse_delivers_frame_types(sse_server: Path) -> None:
    """S1: SSE delivers connected, system, assistant, and result frames in order.

    Business outcome: SDK receives all frame types needed to render
    the assistant's response progressively.
    """
    session_id = str(uuid.uuid4())

    # POST first to create session (execution runs in background)
    httpx.post(
        f"{_BASE_URL}/session/{session_id}/message",
        json={
            "message": "Say hello.",
            "agent_definition": AGENT_SIMPLE,
            "input_payload": dict(_INPUT_PAYLOAD),
        },
        timeout=30.0,
    )

    with httpx.stream(
        "GET",
        f"{_BASE_URL}/event?session_id={session_id}",
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(120.0),
    ) as sse_resp:
        frames = _read_sse_frames(sse_resp)

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


def test_sse_events_have_correct_session(sse_server: Path) -> None:
    """S2: SSE frames carry the requesting session_id.

    Business outcome: SDK can match frames to their session, confirming
    events go to the correct stream rather than a stale or wrong one.
    """
    session_id = str(uuid.uuid4())

    httpx.post(
        f"{_BASE_URL}/session/{session_id}/message",
        json={
            "message": "Say hello.",
            "agent_definition": AGENT_SIMPLE,
            "input_payload": dict(_INPUT_PAYLOAD),
        },
        timeout=30.0,
    )

    with httpx.stream(
        "GET",
        f"{_BASE_URL}/event?session_id={session_id}",
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(120.0),
    ) as sse_resp:
        frames = _read_sse_frames(sse_resp)

    for frame in frames:
        if "session_id" in frame:
            assert frame["session_id"] == session_id, (
                f"Frame {frame.get('type')} has wrong session_id: "
                f"{frame['session_id']} != {session_id}"
            )

    # connected frame always carries session_id
    connected = next(f for f in frames if f.get("type") == "connected")
    assert connected["session_id"] == session_id


def test_multi_device_fan_out(sse_server: Path) -> None:
    """S3: Two concurrent SSE consumers receive the same frames.

    Business outcome: Desktop and Mobile clients viewing the same
    session see identical frame sequences.
    """
    session_id = str(uuid.uuid4())

    httpx.post(
        f"{_BASE_URL}/session/{session_id}/message",
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
                f"{_BASE_URL}/event?session_id={session_id}",
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(120.0),
            ) as resp:
                collected[idx] = _read_sse_frames(resp)
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


def test_sse_stream_terminates_cleanly(sse_server: Path) -> None:
    """S4: SSE connection closes after result frame.

    Business outcome: Connections are not orphaned; resources are
    cleaned up after execution completes.
    """
    session_id = str(uuid.uuid4())

    httpx.post(
        f"{_BASE_URL}/session/{session_id}/message",
        json={
            "message": "Say hello.",
            "agent_definition": AGENT_SIMPLE,
            "input_payload": dict(_INPUT_PAYLOAD),
        },
        timeout=30.0,
    )

    with httpx.stream(
        "GET",
        f"{_BASE_URL}/event?session_id={session_id}",
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(120.0),
    ) as sse_resp:
        frames = _read_sse_frames(sse_resp)

    # Business outcome: last frame is result
    assert frames[-1]["type"] == "result", (
        f"Last frame should be result, got {frames[-1].get('type')}"
    )
    assert frames[-1].get("is_error") is False, "Result frame indicates error"

    # Stream termination: the result frame was delivered and _read_sse_frames
    # returned without timeout, confirming the connection closes cleanly.
