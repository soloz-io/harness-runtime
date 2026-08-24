"""Unit tests for checkpoint restore functionality in harness-runtime."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from core.checkpoint_restore import apply_checkpoint_restore


@pytest.mark.asyncio
async def test_apply_checkpoint_restore_async():
    mock_checkpointer = MagicMock()
    mock_cpt = MagicMock()
    mock_cpt.checkpoint = {
        "v": 1,
        "ts": "2026-08-23T12:00:00Z",
        "channel_values": {"messages": [{"content": "turn 1"}]},
        "channel_versions": {"messages": 1},
    }
    mock_cpt.metadata = {"step": 2, "source": "loop"}

    mock_checkpointer.aget_tuple = AsyncMock(return_value=mock_cpt)
    mock_checkpointer.aput = AsyncMock()

    await apply_checkpoint_restore(
        mock_checkpointer,
        session_id="test-session-123",
        checkpoint_id="cp-target-456",
    )

    mock_checkpointer.aget_tuple.assert_awaited_once_with(
        {
            "configurable": {
                "thread_id": "test-session-123",
                "checkpoint_id": "cp-target-456",
            }
        }
    )

    mock_checkpointer.aput.assert_awaited_once_with(
        {"configurable": {"thread_id": "test-session-123"}},
        mock_cpt.checkpoint,
        mock_cpt.metadata,
        {"messages": 1},
    )


@pytest.mark.asyncio
async def test_apply_checkpoint_restore_not_found():
    mock_checkpointer = MagicMock()
    mock_checkpointer.aget_tuple = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="Checkpoint cp-missing not found"):
        await apply_checkpoint_restore(
            mock_checkpointer,
            session_id="test-session-123",
            checkpoint_id="cp-missing",
        )
