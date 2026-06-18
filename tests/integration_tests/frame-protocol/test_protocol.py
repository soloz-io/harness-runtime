"""Protocol contract tests — validates LiteLLM frame wire format.

Uses the fake NDJSON echo server (no DB, no LLM calls). Validates frame
shape and sequence against the LiteLLM PROTOCOL.md specification, using
real SDK request payloads from simple-bug-fix-invoke-requests.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

FAKE_SERVER = Path(__file__).parent / "fake_server.py"
MOCK_DATA = Path(__file__).parent / "mock" / "simple-bug-fix-invoke-requests.json"
TIMEOUT = 15


def _run_server() -> (
    tuple[subprocess.Popen[bytes], list[dict[str, Any]]]
):
    proc = subprocess.Popen(
        [sys.executable, str(FAKE_SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    return proc, []


def _send(proc: subprocess.Popen[bytes], obj: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(obj).encode() + b"\n")
    proc.stdin.flush()


def _read_frame(proc: subprocess.Popen[bytes]) -> dict[str, Any]:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        raise EOFError("stdout closed unexpectedly")
    return json.loads(line)


def _read_turn(proc: subprocess.Popen[bytes]) -> list[dict[str, Any]]:
    """Read frames until result frame is received."""
    frames: list[dict[str, Any]] = []
    while True:
        frame = _read_frame(proc)
        frames.append(frame)
        if frame.get("type") == "result":
            break
    return frames


def test_initialize_returns_session_id() -> None:
    """control_request {initialize} → control_response {success, session_id}."""
    payloads = json.loads(MOCK_DATA.read_text())
    steps = list(payloads["requests"].values())
    step = steps[0]

    proc, _ = _run_server()
    try:
        _send(proc, {
            "type": "control_request",
            "request_id": "req_1",
            "request": {
                "subtype": "initialize",
                "agent_definition": step["agent_definition"],
                "input_payload": step["input_payload"],
            },
        })

        resp = _read_frame(proc)
        assert resp["type"] == "control_response"
        assert resp["response"]["subtype"] == "success"
        assert isinstance(resp["response"]["session_id"], str)
        assert len(resp["response"]["session_id"]) > 0
    finally:
        proc.stdin.close()
        proc.wait(timeout=TIMEOUT)


def test_full_turn_lifecycle() -> None:
    """user {message} → system/init → stream_event → assistant → result."""
    payloads = json.loads(MOCK_DATA.read_text())
    steps = list(payloads["requests"].values())
    step = steps[0]
    user_content = step["input_payload"]["messages"][0]["content"]

    proc, _ = _run_server()
    try:
        _send(proc, {
            "type": "control_request",
            "request_id": "req_1",
            "request": {
                "subtype": "initialize",
                "agent_definition": step["agent_definition"],
                "input_payload": step["input_payload"],
            },
        })
        init_resp = _read_frame(proc)
        assert init_resp["type"] == "control_response"

        _send(proc, {
            "type": "user",
            "message": {"role": "user", "content": user_content},
            "session_id": None,
            "parent_tool_use_id": None,
        })

        frames = _read_turn(proc)

        assert frames[0]["type"] == "system"
        assert frames[0]["subtype"] == "init"
        assert frames[0]["session_id"] == init_resp["response"]["session_id"]

        stream_events = [f for f in frames if f["type"] == "stream_event"]
        assert len(stream_events) > 0
        for se in stream_events:
            assert se["event"]["type"] == "content_block_delta"
            assert se["event"]["delta"]["type"] == "text_delta"

        assistant_frames = [f for f in frames if f["type"] == "assistant"]
        assert len(assistant_frames) >= 1
        assert any(
            b["type"] == "text"
            for af in assistant_frames
            for b in af["message"]["content"]
        )

        assert frames[-1]["type"] == "result"
        assert frames[-1]["subtype"] == "success"
        assert frames[-1]["duration_ms"] > 0
        assert frames[-1]["is_error"] is False
        assert frames[-1]["num_turns"] >= 1
    finally:
        proc.stdin.close()
        proc.wait(timeout=TIMEOUT)


def test_multi_turn_in_same_session() -> None:
    """Two consecutive user messages → two complete turns, same session."""
    payloads = json.loads(MOCK_DATA.read_text())
    steps = list(payloads["requests"].values())[:2]

    proc, _ = _run_server()
    try:
        _send(proc, {
            "type": "control_request",
            "request_id": "req_1",
            "request": {
                "subtype": "initialize",
                "agent_definition": steps[0]["agent_definition"],
                "input_payload": steps[0]["input_payload"],
            },
        })
        init_resp = _read_frame(proc)
        session_id = init_resp["response"]["session_id"]

        for step in steps:
            content = step["input_payload"]["messages"][0]["content"]
            _send(proc, {
                "type": "user",
                "message": {"role": "user", "content": content},
                "session_id": session_id,
                "parent_tool_use_id": None,
            })
            frames = _read_turn(proc)
            assert frames[-1]["type"] == "result"
            assert frames[-1]["session_id"] == session_id
            assert frames[-1]["result"] == f"echo: {content}"
    finally:
        proc.stdin.close()
        proc.wait(timeout=TIMEOUT)


def test_all_mock_data_steps_round_trip() -> None:
    """Every step in simple-bug-fix-invoke-requests.json round-trips correctly."""
    payloads = json.loads(MOCK_DATA.read_text())
    steps = list(payloads["requests"].values())

    proc, _ = _run_server()
    try:
        for i, step in enumerate(steps):
            _send(proc, {
                "type": "control_request",
                "request_id": f"req_{i}",
                "request": {
                    "subtype": "initialize",
                    "agent_definition": step["agent_definition"],
                    "input_payload": step["input_payload"],
                },
            })
            init_resp = _read_frame(proc)
            assert init_resp["response"]["subtype"] == "success"

            content = step["input_payload"]["messages"][0]["content"]
            _send(proc, {
                "type": "user",
                "message": {"role": "user", "content": content},
                "session_id": init_resp["response"]["session_id"],
                "parent_tool_use_id": None,
            })

            frames = _read_turn(proc)
            assert frames[-1]["type"] == "result"
            assert frames[-1]["result"] == f"echo: {content}"
    finally:
        proc.stdin.close()
        proc.wait(timeout=TIMEOUT)


def test_interrupt_acknowledged() -> None:
    """control_request {interrupt} → control_response {success}."""
    proc, _ = _run_server()
    try:
        _send(proc, {
            "type": "control_request",
            "request_id": "req_interrupt",
            "request": {"subtype": "interrupt"},
        })
        resp = _read_frame(proc)
        assert resp["type"] == "control_response"
        assert resp["response"]["subtype"] == "success"
    finally:
        proc.stdin.close()
        proc.wait(timeout=TIMEOUT)
