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
from typing import Any, Optional

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
    source: str = "deepagents",
    checkpoint_id: Optional[str] = None,
) -> None:
    """Insert new messages into chat_messages.

    Called exactly once per batch of new messages produced by the running
    graph.  Duplicates are silently ignored via ON CONFLICT DO NOTHING.

    The ``source`` parameter distinguishes the origin of messages:
    - ``'deepagents'`` (default) — orchestrator messages from root namespace
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
                # Continue the session's existing numbering, rather than trusting
                # the caller's in-memory count.
                #
                # `offset` is the handler's own `values_messages_count`, which
                # lives in ExecutionState and therefore starts again at 0 every
                # time a Session is constructed — on any sandbox restart, and on
                # every checkpoint restore (which deliberately discards the
                # in-memory session). The result is a `sequence` that restarts
                # mid-conversation: sequence 28 appears five times in one real
                # session here.
                #
                # That matters because sequence is not decoration. The restore
                # endpoint truncates with `sequence > keepUpToSequence`, so a
                # repeated numbering makes "delete everything after this message"
                # delete messages from unrelated earlier turns that happen to
                # carry a higher number.
                #
                # The database already knows the answer, and it is the only
                # participant that survives a restart. Read inside the same
                # connection as the inserts that follow so nothing can interleave
                # between the two: within a session, writes are sequential (the
                # executor dispatches events one at a time), and the ON CONFLICT
                # below still dedupes by message id regardless.
                cur.execute(
                    "SELECT COALESCE(MAX(sequence), -1) + 1 FROM chat_messages WHERE session_id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
                base = row[0] if row and row[0] is not None else 0
                # Keep the caller's offset when it is already ahead — a batch
                # written before any row exists (base 0) still numbers from where
                # the caller thinks it is, preserving order within that batch.
                if offset > base:
                    base = offset

                for i, msg in enumerate(messages):
                    role = ROLE_MAP.get(msg.get("type", ""), "assistant")
                    msg_id = uuid.uuid4().hex
                    cur.execute(
                        """
                        INSERT INTO chat_messages
                            (id, session_id, role, content, message, sequence, source, checkpoint_id)
                        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                        ON CONFLICT (session_id, (message->>'id')) DO NOTHING
                        """,
                        (
                            msg_id,
                            session_id,
                            role,
                            Jsonb(msg.get("content", "")),
                            Jsonb(msg),
                            base + i,
                            source,
                            # The checkpoint this batch was written at, so the
                            # UI can offer "restore to this message" as an
                            # association rather than a positional guess. None
                            # when the caller has no checkpointer (specialists
                            # run with checkpointer=None).
                            checkpoint_id,
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
    workspace_id: str = "",
    app_id: Optional[str] = None,
) -> None:
    """Insert or update agent output files.

    Projected from the ``last_files`` state key accumulated during graph
    execution (both orchestrator and subagent files).  Callers invoke this
    alongside ``write_chat_messages`` so that files are persisted for the
    REST history path, not just SSE streaming.

    ``agent_output_files.session_id`` is a **scope key**, not necessarily the
    executing session:
    - Builder sessions (``workspace_id == app_id``): every file is keyed by
      the app id, so the whole app's artifacts are shared across sessions.
    - Playground sessions: files under ``.global/`` are keyed by the app id
      (app-wide artifacts); everything else is keyed by the session id.
    - Otherwise: files are keyed by the session id (per-session isolation).

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
                    scope_key = _file_scope_key(filepath, session_id, workspace_id, app_id)
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
                            scope_key,
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


def _file_scope_key(
    filepath: str,
    session_id: str,
    workspace_id: str,
    app_id: Optional[str],
) -> str:
    """Return the ``agent_output_files.session_id`` scope key for a file.

    See ``write_agent_output_files`` docstring for the rules.  The ``.global/``
    path prefix is preserved in the stored ``filepath`` so app-global and
    session-scoped rows never collide on the (session_id, filepath) key.
    """
    if app_id and workspace_id == app_id:
        return app_id
    if app_id and filepath.startswith(".global/"):
        return app_id
    return session_id


def stamp_checkpoint_id(pool: ConnectionPool, session_id: str, checkpoint_id: str) -> None:
    """Record which checkpoint this turn's messages were written at.

    Applied AFTER the turn rather than during it, because the checkpoint id only
    exists once the superstep that produced these messages has been committed —
    the stream events that trigger the writes carry no reference to it.

    Every row for this session still missing a checkpoint id belongs to the turn
    that just finished: writes are serialised per session, and a previous turn
    stamped its own rows on the way out. So the `IS NULL` predicate is the
    association, not an approximation of it.

    Best-effort. A message whose checkpoint id is missing degrades to the
    positional pairing the UI already falls back on; failing the turn over it
    would trade a cosmetic loss for a real one.
    """
    if not checkpoint_id:
        return
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE chat_messages
                       SET checkpoint_id = %s
                     WHERE session_id = %s
                       AND checkpoint_id IS NULL
                    """,
                    (checkpoint_id, session_id),
                )
    except Exception:
        logger.warning(
            "stamp_checkpoint_id_failed",
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            exc_info=True,
        )
