"""Write chat_messages projection from live execution messages.

The harness runtime is the sole writer of chat_messages.  Messages are
projected from the in-memory LangChain BaseMessage objects *before* they
are serialized to checkpoints and *before* SSE is emitted to the frontend.

Ownership boundary
------------------
- Runtime owns:  execution, checkpoints, chat_messages (this module)
- SDK owns:      chat_sessions (metadata: tenant_id, workflow_id, title)
- DB:            no FK between chat_messages and chat_sessions — logical only

Invariant
---------
Messages are projected **exactly once** from their live in-memory
representation.  They are never re-projected from deserialized checkpoint
state.  The caller tracks which messages have been seen via
``_values_messages_count`` in the execution state dict.
"""

import uuid
from typing import Any

import structlog
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

logger = structlog.get_logger(__name__)

# Maps LangChain message .type values to chat_messages.role values.
ROLE_MAP: dict[str, str] = {
    "human": "user",
    "ai": "assistant",
    "system": "system",
    "tool": "tool",
}


def write_chat_messages(
    pool: ConnectionPool,
    session_id: str,
    messages: list[dict[str, Any]],
    offset: int,
) -> None:
    """Insert new messages into chat_messages.

    Called exactly once per batch of new messages produced by the running
    graph.  Duplicates are silently ignored via ON CONFLICT DO NOTHING.

    Failures are logged but never raised — the checkpoint is the authoritative
    execution state; this is a read-model projection.
    """
    if not messages:
        return

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                for i, msg in enumerate(messages):
                    role = ROLE_MAP.get(msg.get("type", ""), "assistant")
                    msg_id = uuid.uuid4().hex
                    cur.execute(
                        """
                        INSERT INTO chat_messages
                            (id, session_id, role, content, message, sequence, source)
                        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, 'runtime')
                        ON CONFLICT (session_id, (message->>'id')) DO NOTHING
                        """,
                        (
                            msg_id,
                            session_id,
                            role,
                            Jsonb(msg.get("content", "")),
                            Jsonb(msg),
                            offset + i,
                        ),
                    )
                    if cur.rowcount == 0:
                        logger.warning(
                            "write_chat_messages_duplicate_skipped",
                            session_id=session_id,
                            msg_id=msg_id,
                            role=role,
                        )
                    else:
                        logger.debug(
                            "write_chat_messages_inserted",
                            session_id=session_id,
                            msg_id=msg_id,
                            role=role,
                            sequence=offset + i,
                        )
    except Exception:
        logger.exception(
            "write_chat_messages_failed",
            session_id=session_id,
            message_count=len(messages),
        )
