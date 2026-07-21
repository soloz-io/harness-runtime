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
from pathlib import Path
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

_MEDIA_TYPES: dict[str, str] = {
    ".md": "text/markdown",
    ".json": "application/json",
    ".txt": "text/plain",
    ".html": "text/html",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".csv": "text/csv",
}


def write_chat_messages(
    pool: ConnectionPool,
    session_id: str,
    messages: list[dict[str, Any]],
    offset: int,
    source: str = "runtime",
) -> None:
    """Insert new messages into chat_messages.

    Called exactly once per batch of new messages produced by the running
    graph.  Duplicates are silently ignored via ON CONFLICT DO NOTHING.

    The ``source`` parameter distinguishes the origin of messages:
    - ``'runtime'`` (default) — orchestrator messages from root namespace
    - ``'subagent'`` — sub-agent internal reasoning from sub-agent namespace
    - ``'stream'`` — SDK-initiated inserts

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
                        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                        ON CONFLICT (session_id, (message->>'id')) DO NOTHING
                        """,
                        (
                            msg_id,
                            session_id,
                            role,
                            Jsonb(msg.get("content", "")),
                            Jsonb(msg),
                            offset + i,
                            source,
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


def write_agent_output_files(
    pool: ConnectionPool,
    session_id: str,
    files: dict[str, dict[str, str]],
) -> None:
    """Insert or update agent output files.

    Projected from the ``last_files`` state key accumulated during graph
    execution (both orchestrator and subagent files).  Callers invoke this
    alongside ``write_chat_messages`` so that files are persisted for the
    REST history path, not just SSE streaming.

    Upsert semantics: if a row with the same (session_id, filepath) already
    exists, its content and format are updated.
    """
    if not files:
        return

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                for file_path, file_info in files.items():
                    filepath = file_path.lstrip("/")
                    if filepath.startswith("home/ubuntu/"):
                        filepath = filepath[12:]
                    elif filepath.startswith("workspace/"):
                        filepath = filepath[10:]
                    filename = Path(filepath).name
                    if filepath.startswith("skills/") and len(Path(filepath).parts) > 2:
                        # Strip the 'skills/' prefix to preserve the intermediate folders
                        filename = filepath[7:]
                    fmt = "markdown" if filepath.endswith(".md") else "json"
                    ext = Path(filepath).suffix.lower()
                    media_type = _MEDIA_TYPES.get(ext, "text/plain")
                    file_id = uuid.uuid4().hex
                    cur.execute(
                        """
                        INSERT INTO agent_output_files
                            (id, session_id, filename, filepath, content, format, media_type, url)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (session_id, filepath)
                            DO UPDATE SET content = EXCLUDED.content,
                                          format  = EXCLUDED.format,
                                          media_type = EXCLUDED.media_type
                        """,
                        (
                            file_id,
                            session_id,
                            filename,
                            filepath,
                            file_info.get("content", ""),
                            fmt,
                            media_type,
                            "",
                        ),
                    )
    except Exception:
        logger.exception(
            "write_agent_output_files_failed",
            session_id=session_id,
            file_count=len(files),
        )
