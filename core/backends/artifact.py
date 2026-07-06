"""ArtifactBackend: StateBackend + DB-backed cross-session file reads.

Extends deepagents StateBackend to surface files from other sessions
in the same workspace. Queries ``agent_output_files`` JOINed with
``chat_sessions`` by ``workflow_id``. Writes continue through the
inherited StateBackend channel path (unchanged).
"""

from __future__ import annotations

from typing import Any

import structlog

try:
    from deepagents.backends.protocol import FileData, LsResult, ReadResult
    from deepagents.backends.state import StateBackend
except ImportError:
    raise ImportError(
        "deepagents package is required. Install it with: pip install deepagents>=0.2.0"
    ) from None

try:
    from psycopg_pool import ConnectionPool
except ImportError:
    ConnectionPool = None  # type: ignore[assignment,misc]

logger = structlog.get_logger(__name__)


def _file_info(path: str, is_dir: bool = False) -> dict[str, Any]:
    return {"path": path, "is_dir": is_dir, "size": 0, "modified_at": ""}


class ArtifactBackend(StateBackend):
    """StateBackend that also surfaces cross-session files from ``agent_output_files``.

    Reads:
        Check ``agent_output_files JOIN chat_sessions`` for files in the same
        workspace (``workflow_id``).  Falls through to the inherited
        StateBackend ``read()`` for the current session's channel files.

    Writes / edits / greps / globs:
        All inherited from ``StateBackend`` unchanged — they write to the
        LangGraph ``state["files"]`` channel, which triggers the existing
        values-event → ``message_writer.py`` → ``agent_output_files`` DB path.
    """

    def __init__(
        self,
        workspace_id: str,
        session_id: str,
        pool: ConnectionPool,
    ) -> None:
        super().__init__()
        self.workspace_id = workspace_id
        self.session_id = session_id
        self._pool = pool

    # ------------------------------------------------------------------
    # Internal DB query helpers
    # ------------------------------------------------------------------

    def _query_db(self, path: str) -> tuple | None:
        """Return the most recent file content for *path* across the workspace,
        excluding the current session (already in StateBackend)."""
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT aof.content
                        FROM agent_output_files aof
                        JOIN chat_sessions cs ON aof.session_id = cs.id
                        WHERE cs.workflow_id = %s
                          AND aof.filepath = %s
                          AND aof.session_id != %s
                        ORDER BY aof.created_at DESC
                        LIMIT 1
                        """,
                        (self.workspace_id, path, self.session_id),
                    )
                    return cur.fetchone()
        except Exception:
            logger.exception("artifact_backend_db_read_failed", path=path)
            return None

    def _query_db_ls(self, path: str) -> list[dict[str, Any]]:
        """Return distinct file entries under *path* from other sessions."""
        prefix = path if path.endswith("/") else path + "/"
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT aof.filepath
                        FROM agent_output_files aof
                        JOIN chat_sessions cs ON aof.session_id = cs.id
                        WHERE cs.workflow_id = %s
                          AND aof.filepath LIKE %s
                          AND aof.session_id != %s
                        """,
                        (self.workspace_id, prefix + "%", self.session_id),
                    )
                    return [_file_info(row[0]) for row in cur.fetchall()]
        except Exception:
            logger.exception("artifact_backend_db_ls_failed", path=path)
            return []

    # ------------------------------------------------------------------
    # Overridden BackendProtocol methods
    # ------------------------------------------------------------------

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        row = self._query_db(file_path)
        if row:
            return ReadResult(
                file_data=FileData(content=row[0], encoding="utf-8"),
            )
        return super().read(file_path, offset=offset, limit=limit)

    def ls(self, path: str) -> LsResult:
        db_entries = self._query_db_ls(path)
        state_result = super().ls(path)
        state_entries = state_result.entries or []

        seen: set[str] = {e["path"] for e in state_entries}
        to_add = [e for e in db_entries if e["path"] not in seen]

        if to_add:
            merged = sorted(
                state_entries + to_add,
                key=lambda x: x["path"],
            )
            return LsResult(entries=merged)
        return state_result
