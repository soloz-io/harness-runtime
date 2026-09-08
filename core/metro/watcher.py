"""
Metro HMR watcher — an outbound *client* connection to Metro's own
``/hot`` socket, not a route anything connects to.

opencode's own bridge only ever observes HMR frames because its `/hot`
FastAPI route is *proxying* a real browser's connection through it
(ADR-033 §4) — the browser drives traffic, the bridge inspects it in
transit. Exposing an equivalent `@router.websocket("/hot")` here would sit
idle forever, since the externally-facing preview pipeline is explicitly
out of scope for this phase (nothing would ever connect to it). Instead,
harness-runtime opens its own connection directly to Metro as a client,
purely to watch for the ``update-done`` frame — no proxying, no external
exposure.
"""

import asyncio
import json

import structlog

from core.metro.checkpoint_trigger import schedule_checkpoint
from core.metro.config import HAS_WEBSOCKETS, METRO_URL
from core.workspace_context import get_active_workspace_id

logger = structlog.get_logger(__name__)

_watcher_task: asyncio.Task | None = None

_RECONNECT_DELAY_SECONDS = 2.0


def _on_frame(message: str, root_dir: str) -> None:
    try:
        frame = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return
    if not (isinstance(frame, dict) and frame.get("type") == "update-done"):
        return
    workspace_id = get_active_workspace_id()
    if not workspace_id:
        logger.warning("metro_update_done_no_active_workspace")
        return
    schedule_checkpoint(workspace_id, root_dir)


async def _watch_loop(root_dir: str) -> None:
    import websockets

    while True:
        try:
            async with websockets.connect(f"{METRO_URL}/hot") as ws:
                logger.info("metro_hmr_watcher_connected")
                async for message in ws:
                    if isinstance(message, str):
                        _on_frame(message, root_dir)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("metro_hmr_watcher_reconnecting", error=str(e))
        await asyncio.sleep(_RECONNECT_DELAY_SECONDS)


def ensure_watcher_running(root_dir: str = "/workspace") -> None:
    """Start the HMR watcher task if it isn't already running. Safe to
    call repeatedly and before Metro itself is up — the connect loop
    retries on its own."""
    global _watcher_task
    if not HAS_WEBSOCKETS:
        logger.warning("metro_hmr_watcher_unavailable_no_websockets_lib")
        return
    if _watcher_task is not None and not _watcher_task.done():
        return
    _watcher_task = asyncio.create_task(_watch_loop(root_dir))
