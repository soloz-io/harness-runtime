"""Apply the agent-side half of an undo when a sandbox starts (ADR-035).

An undo has two halves — the files on disk and the agent's own memory — and
both are recorded as a one-shot pin on `chat_sessions` rather than performed
against a running pod. The workspace pin is consumed by the SDK when it creates
the pod; this is the other one, consumed here, because rewinding a LangGraph
thread needs the checkpointer and that lives in this process.

Why a pin and not a call: the workspace restore deletes the sandbox (a restored
workspace IS a new pod, zero-ops ADR-052 §14.3), so an undo that also phoned the
sandbox to rewind its thread could only work while a pod happened to be alive.
A second undo had nothing to call and failed outright. Recording both halves and
applying them at startup makes an undo pure bookkeeping: it works with no
sandbox running, and files and memory land on the same point together.
"""

from typing import Any, Optional

import structlog

from core.checkpoint_restore import apply_checkpoint_restore

logger = structlog.get_logger(__name__)


def _read_pin(pool: Any, session_id: str) -> Optional[str]:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pending_agent_checkpoint_id FROM chat_sessions WHERE id = %s",
                (session_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return row[0] or None


def _clear_pin(pool: Any, session_id: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_sessions SET pending_agent_checkpoint_id = NULL WHERE id = %s",
                (session_id,),
            )


async def apply_pending_agent_restore(
    checkpointer: Any,
    pool: Any,
    session_id: str,
) -> bool:
    """Rewind this thread if an undo asked for it. Returns True if it did.

    Cleared only AFTER the rewind has been written. Clearing first would let a
    pod that died in between drop the undo silently, leaving the user with the
    conversation they asked to discard and no control left to ask again —
    the restore control disappears with the messages it was attached to.
    Applying twice is harmless: forking the same target again produces another
    head carrying identical state, so retry is the safe direction to fail in.
    """
    if pool is None or checkpointer is None:
        return False

    try:
        checkpoint_id = _read_pin(pool, session_id)
    except Exception:
        # A pin that cannot be read must not take the turn down with it. The
        # user asked for a conversation, and refusing to serve it because an
        # undo could not be looked up is the worse failure.
        logger.warning("pending_agent_restore_read_failed", session_id=session_id, exc_info=True)
        return False

    if not checkpoint_id:
        return False

    await apply_checkpoint_restore(checkpointer, session_id, checkpoint_id)
    _clear_pin(pool, session_id)
    logger.info(
        "pending_agent_restore_applied",
        session_id=session_id,
        checkpoint_id=checkpoint_id,
    )
    return True
