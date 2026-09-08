"""
Debounced checkpoint trigger — ported from opencode's
``opencode_event_bridge/checkpoints.py``'s ``schedule_checkpoint()``.

Called from the ``/hot`` WebSocket relay (``core.metro.proxy``) whenever
Metro's own HMR protocol reports ``update-done`` — the same point a human
watching the preview would see the change land (ADR-033 §6.1). Debounced
1.5s trailing so a burst of saves within one agent turn produces one
commit, not one per file.

RETAINED AS A NO-OP, deliberately.

The action this used to take — persisting the workspace — is now entirely the
platform's (ADR-052 §14): the PVC holds the live tree and the workspace-sync
sidecar snapshots it on its own schedule and at teardown. Nothing in this
process should know that layer exists, so an HMR frame no longer triggers
anything here.

The trigger and its debounce are kept because the HMR signal itself is still
the right hook for anything genuinely agent-owned that wants a "the preview
just updated" boundary. If nothing claims it, this module should be deleted
rather than left as ceremony.
"""

import asyncio

import structlog

from core.metro.config import CHECKPOINT_DEBOUNCE_SECONDS

logger = structlog.get_logger(__name__)

_debounce_tasks: dict[str, asyncio.Task] = {}


async def _fire(workspace_id: str, root_dir: str) -> None:
    try:
        await asyncio.sleep(CHECKPOINT_DEBOUNCE_SECONDS)
        # Nothing to do: see the module docstring.
        return
    except asyncio.CancelledError:
        pass
    except Exception:
        # before raising — this is a fire-and-forget task nobody awaits, so
        # without an explicit log here the exception would only surface via
        # asyncio's own easy-to-miss "Task exception was never retrieved"
        # path. Re-raised so it's still visible there too, not swallowed.
        logger.error("checkpoint_trigger_failed", workspace_id=workspace_id, exc_info=True)
        raise
    finally:
        _debounce_tasks.pop(workspace_id, None)


def schedule_checkpoint(workspace_id: str, root_dir: str = "/workspace") -> None:
    """Debounce rapid HMR updates (multi-file saves in one turn) into one
    commit+sync. Cancels any pending debounce for this workspace and
    reschedules."""
    pending = _debounce_tasks.get(workspace_id)
    if pending and not pending.done():
        pending.cancel()
    _debounce_tasks[workspace_id] = asyncio.create_task(_fire(workspace_id, root_dir))
