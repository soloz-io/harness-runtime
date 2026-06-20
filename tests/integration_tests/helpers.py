"""Shared NDJSON I/O and DB helpers for harness-runtime integration tests.

Usage:
    from tests.integration_tests.helpers import send, read_frame, read_turn, ...
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from pathlib import Path
from typing import Any

import psycopg

# ---------------------------------------------------------------------------
# NDJSON frame I/O
# ---------------------------------------------------------------------------


def send(proc: subprocess.Popen[bytes], obj: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(obj).encode() + b"\n")
    proc.stdin.flush()


def read_frame(proc: subprocess.Popen[bytes]) -> dict[str, Any]:
    """Read a single NDJSON frame from stdout (timeout ~100s)."""
    assert proc.stdout is not None
    for _ in range(200):
        if proc.poll() is not None:
            time.sleep(0.1)
            leftover = proc.stdout.read()
            raise EOFError(
                f"CLI exited (code {proc.returncode})"
                + (f" leftover stdout: {leftover.decode()[:500]}" if leftover else "")
            )
        r, _, _ = select.select([proc.stdout], [], [], 0.5)
        if r:
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.1)
                leftover = proc.stdout.read()
                raise EOFError(
                    f"stdout closed (exit code {proc.poll()})"
                    + (f" leftover stdout: {leftover.decode()[:500]}" if leftover else "")
                )
            return json.loads(line)
    raise TimeoutError("No frame received within 100s")


def read_turn(proc: subprocess.Popen[bytes]) -> list[dict[str, Any]]:
    """Read frames until result."""
    frames: list[dict[str, Any]] = []
    while True:
        frame = read_frame(proc)
        frames.append(frame)
        if frame.get("type") == "result":
            break
    return frames


def read_frame_fast(proc: subprocess.Popen[bytes], timeout_sec: float = 60.0) -> dict[str, Any]:
    """Read a single NDJSON frame with configurable timeout."""
    deadline = time.monotonic() + timeout_sec
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            time.sleep(0.1)
            leftover = proc.stdout.read()
            raise EOFError(
                f"CLI exited (code {proc.returncode})"
                + (f" leftover stdout: {leftover.decode()[:500]}" if leftover else "")
            )
        r, _, _ = select.select([proc.stdout], [], [], 0.5)
        if r:
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.1)
                leftover = proc.stdout.read()
                raise EOFError(
                    f"stdout closed (exit code {proc.poll()})"
                    + (f" leftover stdout: {leftover.decode()[:500]}" if leftover else "")
                )
            return json.loads(line)
    raise TimeoutError(f"No frame received within {timeout_sec}s")


def read_turn_fast(
    proc: subprocess.Popen[bytes], timeout_sec: float = 60.0
) -> list[dict[str, Any]]:
    """Read frames until result with configurable timeout."""
    frames: list[dict[str, Any]] = []
    while True:
        frame = read_frame_fast(proc, timeout_sec)
        frames.append(frame)
        if frame.get("type") == "result":
            break
    return frames


# ---------------------------------------------------------------------------
# Artifact capture
# ---------------------------------------------------------------------------


def save_artifacts(
    artifact_dir: Path,
    frames: list[dict[str, Any]],
    stderr: bytes = b"",
) -> None:
    (artifact_dir / "frames.json").write_text(json.dumps(frames, indent=2, default=str))
    if stderr:
        (artifact_dir / "stderr.log").write_bytes(stderr)


# ---------------------------------------------------------------------------
# Higher-level orchestration (ask-user lifecycle)
# ---------------------------------------------------------------------------

_INPUT_PAYLOAD: dict[str, Any] = {
    "messages": [{"role": "user", "content": "Help me make a decision."}]
}


def initialize(harness: subprocess.Popen[bytes], agent: dict[str, Any]) -> str:
    """Send initialize, return session_id."""
    send(
        harness,
        {
            "type": "control_request",
            "request_id": "req_init",
            "request": {
                "subtype": "initialize",
                "agent_definition": agent,
                "input_payload": dict(_INPUT_PAYLOAD),
            },
        },
    )
    resp = read_frame(harness)
    assert resp["type"] == "control_response"
    assert resp["response"]["subtype"] == "success"
    return resp["response"]["session_id"]


def send_user(harness: subprocess.Popen[bytes], content: str = "I need help.") -> None:
    send(
        harness,
        {
            "type": "user",
            "message": {"role": "user", "content": content},
            "session_id": None,
            "parent_tool_use_id": None,
        },
    )


def initialize_and_assert_interrupt(
    harness: subprocess.Popen[bytes],
    agent: dict[str, Any],
    artifact_dir: Path | None = None,
    max_attempts: int = 5,
    user_content: str = "I need help.",
) -> tuple[str, list[dict[str, Any]]]:
    """Initialize, send user, and assert interrupt result.

    Retries with a fresh session if the LLM doesn't call ask_user
    or if the LLM takes too long (deepseek-v4-flash is
    non-deterministic for tool calling).
    """
    for attempt in range(1, max_attempts + 1):
        session_id = initialize(harness, agent)

        send_user(harness, user_content)
        try:
            frames = read_turn_fast(harness)
        except (TimeoutError, EOFError) as exc:
            if artifact_dir:
                (artifact_dir / f"retry_{attempt}_error.txt").write_text(
                    f"{type(exc).__name__}: {exc}\n"
                )
            continue

        result = frames[-1]
        if result["type"] == "result" and result["subtype"] == "interrupted":
            assert result["interrupt"] is not None
            assert result["interrupt"]["type"] == "ask_user"
            assert len(result["interrupt"]["questions"]) > 0
            return session_id, frames

        if artifact_dir:
            (artifact_dir / f"retry_{attempt}_frames.json").write_text(
                json.dumps(frames, indent=2, default=str)
            )

    pytest.fail(
        f"LLM did not call ask_user after {max_attempts} attempts. "
        "deepseek-v4-flash is non-deterministic for tool calling."
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_DATABASE_URL = os.environ.get("DATABASE_URL", "")


def count_checkpoints(session_id: str) -> int:
    with psycopg.connect(_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s",
                (session_id,),
            )
            return cur.fetchone()[0]


def get_checkpoint_metadata(session_id: str) -> dict[str, Any] | None:
    with psycopg.connect(_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata FROM checkpoints "
                "WHERE thread_id = %s ORDER BY checkpoint_id DESC LIMIT 1",
                (session_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def get_checkpoint_ids(session_id: str) -> list[str]:
    with psycopg.connect(_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id",
                (session_id,),
            )
            return [row[0] for row in cur.fetchall()]
