"""Apply the restore this sandbox was created for (ADR-035).

A restore is a pod creation: the SDK deletes the old sandbox and provisions a
replacement with both halves pinned — the workspace snapshot as
`workspacePersistence.checkpointId`, which the sync sidecar applies before this
container starts, and the LangGraph checkpoint as `RESTORE_CHECKPOINT_ID` in
this process's environment.

So the rewind is startup configuration, read once from the env, not a database
row this process has to remember to poll and clear. The pins it used to read
(`pending_agent_checkpoint_id`, `pending_restore_checkpoint_id`) are gone: they
existed only because the restore and the provision were two separate HTTP
requests with nothing to pass an argument between them. That cost two real
defects — a destroyed sandbox left advertising itself as live, and the two pins
clearing at different moments, which showed a restore to users who never asked
for one.

Applied once per process. A pod created by a restore exists to BE that restore,
so there is nothing to consume and nothing to clear; restarting the container
re-applies the same rewind, which is idempotent — forking the same target again
yields another head carrying identical state.
"""

import os
from typing import Any, Optional

import structlog

from core.checkpoint_restore import apply_checkpoint_restore

logger = structlog.get_logger(__name__)

_applied = False


def pending_restore_checkpoint_id() -> Optional[str]:
    """The checkpoint this sandbox was created to rewind to, if any."""
    return os.environ.get("RESTORE_CHECKPOINT_ID") or None


async def apply_pending_agent_restore(checkpointer: Any, session_id: str) -> bool:
    """Rewind this thread if the pod was created by a restore. True if it did.

    Guarded so a second message in the same pod does not rewind again — the
    checkpoint is startup config and stays in the environment for the life of
    the container, but it describes a state this thread has already been moved
    to.
    """
    global _applied
    if _applied or checkpointer is None:
        return False

    checkpoint_id = pending_restore_checkpoint_id()
    if not checkpoint_id:
        return False

    await apply_checkpoint_restore(checkpointer, session_id, checkpoint_id)
    _applied = True
    logger.info(
        "pending_agent_restore_applied",
        session_id=session_id,
        checkpoint_id=checkpoint_id,
    )
    return True
