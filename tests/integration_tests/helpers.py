"""Shared helpers for harness-runtime integration tests.

Usage:
    from tests.integration_tests.helpers import count_checkpoints, read_sse_frames, ...
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import psycopg

# ---------------------------------------------------------------------------
# SSE frame reading
# ---------------------------------------------------------------------------


def read_sse_frames(
    response: httpx.Response,
    *,
    timeout_sec: float = 120.0,
) -> list[dict[str, Any]]:
    """Read SSE stream until result frame, return parsed frame dicts.

    Parses SSE protocol (``event:\\n`` / ``data:\\n``, normalizing
    ``\\r\\n`` to ``\\n``).  Ignores keepalive pings (empty data).
    """
    deadline = time.monotonic() + timeout_sec
    buf = bytearray()
    frames: list[dict[str, Any]] = []

    for chunk in response.iter_bytes():
        buf.extend(chunk)
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
# Artifact capture
# ---------------------------------------------------------------------------


def save_frames(
    artifact_dir: Path,
    frames: list[dict[str, Any]],
) -> None:
    """Save captured frames as JSON for debugging."""
    (artifact_dir / "frames.json").write_text(json.dumps(frames, indent=2, default=str))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        from dotenv import load_dotenv
        from pathlib import Path

        load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")
        url = os.environ.get("DATABASE_URL", "")
    return url


def count_checkpoints(session_id: str) -> int:
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = %s",
                (session_id,),
            )
            return cur.fetchone()[0]


def get_checkpoint_metadata(session_id: str) -> dict[str, Any] | None:
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata FROM checkpoints "
                "WHERE thread_id = %s ORDER BY checkpoint_id DESC LIMIT 1",
                (session_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def get_checkpoint_ids(session_id: str) -> list[str]:
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id",
                (session_id,),
            )
            return [row[0] for row in cur.fetchall()]
