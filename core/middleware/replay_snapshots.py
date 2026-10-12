"""A workspace snapshot after every model call, for a computer that can be replayed.

A share replays a session's computer from the workspace snapshots the
platform takes (waypoint ADR-050 §6, ADR-048). The snapshot mechanism is the
workspace-sync sidecar's on-demand request, unchanged (zero-ops ADR-052 §14);
what this adds is when it is asked for: after each model call of every agent,
named by the id of the message that call produced. That name is the only link
between the conversation and the computer.

A snapshot taken after a model call holds the files written by the tools of
the call before it, and every tool batch is followed by another model call, so
every write is captured by the next one.

Never in the agent's way:

- the request is sent in the background and its answer is not awaited;
- it does not queue: a snapshot already running answers busy and this one is
  dropped (the next model call captures the state instead);
- an unchanged tree is skipped by the sidecar before any image is built;
- any failure is logged and forgotten. A missing snapshot only means the
  replay holds the previous state a little longer.

On only when the session's definition declares replay: the SDK sets
``HARNESS_REPLAY_SNAPSHOTS=1`` in the pod's environment.
"""

import asyncio
import os
import threading
from typing import Any, Optional

import httpx
import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

logger = structlog.get_logger(__name__)

ENV_ENABLED = "HARNESS_REPLAY_SNAPSHOTS"

# The sidecar waits this long for a running snapshot to finish before
# answering busy. One second, not zero: a zero wait races the free slot.
_QUEUE_WAIT_SECONDS = 1
# A snapshot of a large workspace takes a while; the request is in the
# background, so this bounds only how long a dead sidecar holds a connection.
_REQUEST_TIMEOUT_SECONDS = 900.0
DESCRIPTION = "replay"


def enabled() -> bool:
    return os.environ.get(ENV_ENABLED) == "1"


def _checkpoint_url() -> str:
    port = os.environ.get("WORKSPACE_SYNC_PORT", "7070")
    return f"http://127.0.0.1:{port}/checkpoint?wait={_QUEUE_WAIT_SECONDS}"


def snapshot_name(state: Any) -> Optional[str]:
    """The id of the message the model call just produced, or None."""
    messages = (state or {}).get("messages") or []
    last = messages[-1] if messages else None
    if not isinstance(last, AIMessage) or not isinstance(last.id, str) or not last.id:
        return None
    return last.id


def _outcome(resp: httpx.Response, name: str) -> None:
    if resp.status_code == 503:
        logger.debug("replay_snapshot_busy", name=name)
        return
    if resp.status_code >= 400:
        logger.warning(
            "replay_snapshot_refused", name=name, status=resp.status_code, body=resp.text[:200]
        )
        return
    body = (
        resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    )
    logger.debug(
        "replay_snapshot_taken",
        name=name,
        checkpoint=body.get("checkpointId") or None,
        skipped=body.get("skipped"),
    )


async def _request_async(name: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _checkpoint_url(), json={"name": name, "description": DESCRIPTION}
            )
        _outcome(resp, name)
    except Exception as e:  # noqa: BLE001 -- never the agent's problem
        logger.warning("replay_snapshot_failed", name=name, error=str(e))


def _request_sync(name: str) -> None:
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            resp = client.post(_checkpoint_url(), json={"name": name, "description": DESCRIPTION})
        _outcome(resp, name)
    except Exception as e:  # noqa: BLE001 -- never the agent's problem
        logger.warning("replay_snapshot_failed", name=name, error=str(e))


# Strong references to in-flight requests, so the loop does not collect them.
_pending: set[asyncio.Task[None]] = set()


class ReplaySnapshotMiddleware(AgentMiddleware):
    """After each model call, asks the workspace for a snapshot in the background."""

    def after_model(self, state: Any, runtime: Any) -> None:  # noqa: ARG002
        name = snapshot_name(state) if enabled() else None
        if name:
            threading.Thread(target=_request_sync, args=(name,), daemon=True).start()
        return None

    async def aafter_model(self, state: Any, runtime: Any) -> None:  # noqa: ARG002
        name = snapshot_name(state) if enabled() else None
        if name:
            task = asyncio.create_task(_request_async(name))
            _pending.add(task)
            task.add_done_callback(_pending.discard)
        return None
