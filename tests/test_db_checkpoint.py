"""Business integration tests — real CLI, real DB, real LLM.

Tests the harness-runtime CLI as a black box (same as SDK usage):
  - Single subprocess per test module (session-scoped fixture)
  - Send control_request {initialize} + user {message} via stdin
  - Read LiteLLM frames from stdout
  - Validate frame sequence and shape
  - Query PostgreSQL directly for checkpoint persistence

Requires:
  OPENAI_API_KEY   — LLM provider API key

Run with:
  cd tests && docker compose up -d --wait
  python3 -m pytest tests/test_db_checkpoint.py -v --timeout 180
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

MOCK_DATA = Path(__file__).parent / "mock" / "simple-bug-fix-invoke-requests.json"

# Fail hard if requirements not met
_MISSING: list[str] = []
if not os.environ.get("OPENAI_API_KEY") and not os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
    _MISSING.append("OPENAI_API_KEY, DEEPSEEK_API_KEY, or ANTHROPIC_API_KEY")
if _MISSING:
    pytest.fail("Required environment variables:\n  " + "\n  ".join(_MISSING))


# ---------------------------------------------------------------------------
# Session-scoped subprocess
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def harness(artifact_dir: Path) -> subprocess.Popen[bytes]:
    """Start the CLI subprocess once per test, saving artifacts."""
    cli_path = Path(__file__).parent.parent / "cli.py"
    if not cli_path.exists():
        import shutil
        installed = shutil.which("harness-runtime")
        if not installed:
            pytest.fail("harness-runtime not found. Install with: pip install -e .")
        cli_path = Path(installed)

    log_dir = Path(__file__).parent.parent / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, str(cli_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "HARNESS_LOG_FILE": str(log_dir / "harness-cli.log"),
            "HARNESS_LOG_LEVEL": "DEBUG",
            "LLM_MODEL_NAME": os.environ.get("LLM_MODEL_NAME", "deepseek-v4-pro"),
            "OPENAI_API_KEY": os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or "",
            "DATABASE_URL": os.environ.get("DATABASE_URL")
                or "postgresql://waypoint:waypoint@localhost:5433/waypoint_test",
            "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL")
                or "https://api.deepseek.com",
        },
    )

    # Wait for CLI to be ready
    import time
    for _ in range(100):
        if proc.poll() is not None:
            pytest.fail(f"CLI exited prematurely (code {proc.returncode})")
        time.sleep(0.1)

    yield proc

    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    # Save stderr to artifacts after process exits
    if proc.stderr:
        stderr = proc.stderr.read()
        if stderr:
            (artifact_dir / "stderr.log").write_bytes(stderr)


def _send(proc: subprocess.Popen[bytes], obj: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(obj).encode() + b"\n")
    proc.stdin.flush()


def _read_frame(proc: subprocess.Popen[bytes]) -> dict[str, Any]:
    assert proc.stdout is not None
    import select
    import time
    for _ in range(200):
        if proc.poll() is not None:
            # Small delay for stdout buffer to drain before raising
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


def _read_turn(proc: subprocess.Popen[bytes]) -> list[dict[str, Any]]:
    """Read frames until result."""
    frames: list[dict[str, Any]] = []
    while True:
        frame = _read_frame(proc)
        frames.append(frame)
        if frame.get("type") == "result":
            break
    return frames


def _save_artifacts(artifact_dir: Path, frames: list[dict[str, Any]], stderr: bytes = b"") -> None:
    """Save captured frames and stderr to the artifact directory."""
    (artifact_dir / "frames.json").write_text(json.dumps(frames, indent=2, default=str))
    if stderr:
        (artifact_dir / "stderr.log").write_bytes(stderr)


def _count_checkpoints(session_id: str) -> int:
    db_url = os.environ.get("DATABASE_URL") or "postgresql://waypoint:waypoint@localhost:5433/waypoint_test"
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s",
                (session_id,),
            )
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_initialize_and_single_turn(harness: subprocess.Popen[bytes], artifact_dir: Path) -> None:
    """Initialize → user → system/init → result + checkpoints."""
    payloads = json.loads(MOCK_DATA.read_text())
    step = list(payloads["requests"].values())[0]
    user_content = step["input_payload"]["messages"][0]["content"]

    _send(harness, {
        "type": "control_request",
        "request_id": "req_1",
        "request": {
            "subtype": "initialize",
            "agent_definition": step["agent_definition"],
            "input_payload": step["input_payload"],
        },
    })
    init_resp = _read_frame(harness)
    assert init_resp["type"] == "control_response"
    assert init_resp["response"]["subtype"] == "success"
    session_id = init_resp["response"]["session_id"]
    assert len(session_id) > 0

    _send(harness, {
        "type": "user",
        "message": {"role": "user", "content": user_content},
        "session_id": None,
        "parent_tool_use_id": None,
    })
    frames = _read_turn(harness)
    _save_artifacts(artifact_dir, frames)

    assert frames[0]["type"] == "system"
    assert frames[0]["subtype"] == "init"
    assert frames[0]["session_id"] == session_id

    assert frames[-1]["type"] == "result"
    assert frames[-1]["session_id"] == session_id

    db_url = os.environ.get("DATABASE_URL") or "postgresql://waypoint:waypoint@localhost:5433/waypoint_test"
    _count_checkpoints(session_id)


def test_user_without_initialize_returns_error(harness: subprocess.Popen[bytes], artifact_dir: Path) -> None:
    """User message without initialize → error result frame."""
    _send(harness, {
        "type": "user",
        "message": {"role": "user", "content": "hello"},
        "session_id": None,
        "parent_tool_use_id": None,
    })
    frames = _read_turn(harness)
    _save_artifacts(artifact_dir, frames)
    assert frames[-1]["type"] == "result"
    assert frames[-1]["is_error"] is True
