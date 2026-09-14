"""In-place rewind of a LangGraph thread to a prior checkpoint."""

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import copy_checkpoint, create_checkpoint

logger = structlog.get_logger(__name__)


async def apply_checkpoint_restore(
    checkpointer: Any,
    session_id: str,
    checkpoint_id: str,
) -> None:
    """Rewind the LangGraph thread to the given checkpoint.

    Loads the target CheckpointTuple and writes it as the HEAD of the
    thread under a new checkpoint ID.
    """
    if checkpointer is None:
        raise ValueError("Checkpointer is not initialized")

    # `checkpoint_ns` is required, not optional.
    #
    # The read paths tolerate its absence — AsyncPostgresSaver.aget_tuple and
    # .alist both do `.get("checkpoint_ns", "")` — but `aput` does
    # `configurable.pop("checkpoint_ns")` with NO default, so omitting it raises
    # KeyError('checkpoint_ns'). That surfaced to the user as a bare 500 from
    # the restore endpoint with `{"detail": "'checkpoint_ns'"}` as its only
    # explanation, and made every checkpoint restore fail before it could reach
    # the workspace half.
    #
    # The empty string is the root namespace, which is where a thread's own
    # checkpoints live; a non-empty value addresses a subgraph's.
    source_config: RunnableConfig = {
        "configurable": {
            "thread_id": session_id,
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
        }
    }

    if hasattr(checkpointer, "aget_tuple"):
        cpt = await checkpointer.aget_tuple(source_config)
    else:
        cpt = checkpointer.get_tuple(source_config)

    if cpt is None:
        raise ValueError(f"Checkpoint {checkpoint_id} not found for session {session_id}")

    checkpoint = cpt.checkpoint if hasattr(cpt, "checkpoint") else cpt
    metadata = dict(getattr(cpt, "metadata", {}) or {})
    channel_versions = (
        checkpoint.get("channel_versions", {}) if isinstance(checkpoint, dict) else {}
    )

    # FORK the target into a new checkpoint; never re-put the target itself.
    #
    # `aput` derives the row's primary key from `checkpoint["id"]`, and
    # UPSERT_CHECKPOINTS_SQL conflicts on (thread_id, checkpoint_ns,
    # checkpoint_id) with `DO UPDATE SET checkpoint, metadata` — it does not
    # touch parent_checkpoint_id. So handing `aput` the tuple exactly as it was
    # read writes the target checkpoint ON TOP OF ITSELF: no new row, no new
    # head, and on the insert path a NULL parent that orphans it.
    #
    # That is what this function used to do, and it made restore a silent
    # no-op. `aget_tuple` picks the head with `ORDER BY checkpoint_id DESC
    # LIMIT 1`, so the thread stayed on the checkpoint it was already on. The
    # conversation rows were deleted from `chat_messages` while the agent kept
    # the full pre-restore state, and the next turn replayed the deleted
    # messages and wrote them back — verified live: seven rows, including two
    # the restore had removed, re-inserted with fresh ids on the next send.
    #
    # `create_checkpoint` mints a fresh uuid6 (`clock_seq=step`), which is
    # time-ordered and therefore sorts above every existing checkpoint — so the
    # copy becomes the head. Passing the TARGET as `checkpoint_id` in the config
    # makes it the new checkpoint's parent, which is what a fork is: same
    # state, new identity, honest lineage.
    step = metadata.get("step")
    forked = create_checkpoint(
        copy_checkpoint(checkpoint), None, step if isinstance(step, int) else -1
    )

    # This checkpoint was written by a restore, not by the graph taking a step.
    # LangGraph's own `update_state` labels a forked checkpoint this way, and
    # anything reading history to explain "why is the thread here" needs it.
    metadata["source"] = "update"
    if isinstance(step, int):
        metadata["step"] = step + 1

    target_config: RunnableConfig = {
        "configurable": {
            "thread_id": session_id,
            "checkpoint_ns": "",
            # The parent — the checkpoint being restored to.
            "checkpoint_id": checkpoint_id,
        }
    }

    if hasattr(checkpointer, "aput"):
        await checkpointer.aput(target_config, forked, metadata, channel_versions)
    else:
        checkpointer.put(target_config, forked, metadata, channel_versions)

    logger.info(
        "checkpoint_restored",
        session_id=session_id,
        checkpoint_id=checkpoint_id,
        forked_checkpoint_id=forked["id"],
    )
