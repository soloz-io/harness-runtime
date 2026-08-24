"""In-place rewind of a LangGraph thread to a prior checkpoint."""

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig

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

    source_config: RunnableConfig = {
        "configurable": {
            "thread_id": session_id,
            "checkpoint_id": checkpoint_id,
        }
    }

    if hasattr(checkpointer, "aget_tuple"):
        cpt = await checkpointer.aget_tuple(source_config)
    else:
        cpt = checkpointer.get_tuple(source_config)

    if cpt is None:
        raise ValueError(f"Checkpoint {checkpoint_id} not found for session {session_id}")

    target_config: RunnableConfig = {
        "configurable": {
            "thread_id": session_id,
        }
    }

    checkpoint = cpt.checkpoint if hasattr(cpt, "checkpoint") else cpt
    metadata = getattr(cpt, "metadata", {}) or {}
    channel_versions = (
        checkpoint.get("channel_versions", {}) if isinstance(checkpoint, dict) else {}
    )

    if hasattr(checkpointer, "aput"):
        await checkpointer.aput(target_config, checkpoint, metadata, channel_versions)
    else:
        checkpointer.put(target_config, checkpoint, metadata, channel_versions)

    logger.info(
        "checkpoint_restored",
        session_id=session_id,
        checkpoint_id=checkpoint_id,
    )
