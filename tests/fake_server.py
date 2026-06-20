#!/usr/bin/env python3
"""Deterministic fake harness-runtime server (pure stdlib).

Speaks the LiteLLM frame protocol over stdin/stdout NDJSON.

- control_request {initialize}  → control_response {success, session_id}
- control_request {interrupt}    → control_response {success}
- user {message}                 → system {init}
                                    stream_event {content_block_delta} (echo text)
                                    assistant {text: echo: <content>}
                                    result {success}
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any


def _write(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _control_success(request_id: Any, **extra: Any) -> None:
    _write(
        {
            "type": "control_response",
            "response": {"request_id": request_id, "subtype": "success", **extra},
        }
    )


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _run_turn(content: Any, session_id: str) -> None:
    prompt = _content_to_text(content)

    _write(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "model": "gpt-4-fake",
            "tools": [],
        }
    )

    for char in prompt:
        _write(
            {
                "type": "stream_event",
                "session_id": session_id,
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": char},
                },
            }
        )

    _write(
        {
            "type": "assistant",
            "message": {
                "model": "gpt-4-fake",
                "content": [{"type": "text", "text": f"echo: {prompt}"}],
            },
            "parent_tool_use_id": None,
            "session_id": session_id,
        }
    )

    _write(
        {
            "type": "result",
            "subtype": "success",
            "session_id": session_id,
            "duration_ms": 1,
            "duration_api_ms": 1,
            "is_error": False,
            "num_turns": 1,
            "total_cost_usd": 0.0,
            "usage": {},
            "result": f"echo: {prompt}",
        }
    )


def main() -> None:
    session_id: str | None = None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        msg_type = obj.get("type")

        if msg_type == "control_request":
            request = obj.get("request", {})
            subtype = request.get("subtype")
            request_id = obj.get("request_id", "")

            if subtype == "initialize":
                session_id = f"sess_fake_{uuid.uuid4().hex[:12]}"
                _control_success(request_id, session_id=session_id)
            elif subtype == "interrupt":
                _control_success(request_id)

        elif msg_type == "user":
            sid = session_id or f"sess_fake_{uuid.uuid4().hex[:12]}"
            content = (obj.get("message") or {}).get("content")
            _run_turn(content, sid)


if __name__ == "__main__":
    main()
