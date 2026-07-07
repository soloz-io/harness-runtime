"""ArtifactBackend: StateBackend + DB-backed cross-session file operations.

Extends deepagents StateBackend to surface files from other sessions
in the same workspace. Queries ``agent_output_files`` JOINed with
``chat_sessions`` by ``workflow_id``. Writes continue through the
inherited StateBackend channel path (unchanged).
"""

from __future__ import annotations

import fnmatch
from typing import Any

import structlog

try:
    from deepagents.backends.protocol import (
        FileData,
        GlobResult,
        GrepMatch,
        GrepResult,
        LsResult,
        ReadResult,
    )
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

    Writes / edits / upload_files:
        All inherited from ``StateBackend`` unchanged — they write to the
        LangGraph ``state["files"]`` channel, which triggers the existing
        values-event → ``message_writer.py`` → ``agent_output_files`` DB path.

    Grep / glob / ls:
        Merge DB results from other sessions in the same workspace with the
        current session's channel results, deduplicated by file path.
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

    def _normalize_db_path(self, path: str) -> str:
        """Strip /workspace/ prefix from agent paths to match DB."""
        if path == "/workspace":
            return ""
        if path.startswith("/workspace/"):
            return path[len("/workspace/") :]
        return path

    def _format_agent_path(self, path: str) -> str:
        """Prepend /workspace/ to DB paths for the agent."""
        if not path.startswith("/workspace"):
            return f"/workspace/{path}"
        return path

    # ------------------------------------------------------------------
    # Internal DB query helpers
    # ------------------------------------------------------------------

    def _query_db(self, path: str) -> tuple | None:
        """Return the most recent file content for *path* across the workspace,
        excluding the current session (already in StateBackend)."""
        db_path = self._normalize_db_path(path)
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT aof.content
                        FROM agent_output_files aof
                        JOIN chat_sessions cs ON aof.session_id = cs.id
                        WHERE cs.workflow_id = %s
                          AND (aof.filepath = %s OR aof.filepath = %s)
                          AND aof.session_id != %s
                        ORDER BY aof.created_at DESC
                        LIMIT 1
                        """,
                        (self.workspace_id, db_path, path, self.session_id),
                    )
                    return cur.fetchone()
        except Exception:
            logger.exception("artifact_backend_db_read_failed", path=path)
            return None

    def _query_db_ls(self, path: str) -> list[dict[str, Any]]:
        """Return distinct file entries under *path* from other sessions."""
        db_path = self._normalize_db_path(path)
        prefix1 = db_path if db_path.endswith("/") or not db_path else db_path + "/"
        prefix2 = path if path.endswith("/") else path + "/"
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT aof.filepath
                        FROM agent_output_files aof
                        JOIN chat_sessions cs ON aof.session_id = cs.id
                        WHERE cs.workflow_id = %s
                          AND (aof.filepath LIKE %s OR aof.filepath LIKE %s)
                          AND aof.session_id != %s
                        """,
                        (self.workspace_id, prefix1 + "%", prefix2 + "%", self.session_id),
                    )
                    return [_file_info(self._format_agent_path(row[0])) for row in cur.fetchall()]
        except Exception:
            logger.exception("artifact_backend_db_ls_failed", path=path)
            return []

    def _query_db_filepaths(self) -> list[str]:
        """Return all distinct filepaths from other sessions in the workspace."""
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT aof.filepath
                        FROM agent_output_files aof
                        JOIN chat_sessions cs ON aof.session_id = cs.id
                        WHERE cs.workflow_id = %s
                          AND aof.session_id != %s
                        """,
                        (self.workspace_id, self.session_id),
                    )
                    return [self._format_agent_path(row[0]) for row in cur.fetchall()]
        except Exception:
            logger.exception("artifact_backend_db_filepaths_failed")
            return []

    def _query_db_grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch]:
        """Search file contents from other sessions for a literal substring.

        Fetches all distinct files from the DB matching the workspace and
        applies path/glob filtering in Python, then searches each file's
        content line by line (same semantics as ``grep_matches_from_files``).
        """
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT ON (aof.filepath) aof.filepath, aof.content
                        FROM agent_output_files aof
                        JOIN chat_sessions cs ON aof.session_id = cs.id
                        WHERE cs.workflow_id = %s
                          AND aof.session_id != %s
                        ORDER BY aof.filepath, aof.created_at DESC
                        """,
                        (self.workspace_id, self.session_id),
                    )
                    rows = cur.fetchall()
        except Exception:
            logger.exception("artifact_backend_db_grep_failed", pattern=pattern)
            return []

        matches: list[GrepMatch] = []
        for filepath, content in rows:
            agent_filepath = self._format_agent_path(filepath)
            if path and not agent_filepath.startswith(path):
                continue
            if glob and not fnmatch.fnmatch(agent_filepath, glob):
                continue
            for line_num, line in enumerate(content.split("\n"), 1):
                if pattern in line:
                    matches.append(
                        GrepMatch(
                            path=agent_filepath,
                            line=line_num,
                            text=line,
                        )
                    )
        return matches

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

    def _glob_match(self, filepath: str, pattern: str) -> bool:
        """Match a filepath against a glob pattern, supporting ``**/``."""
        if pattern.startswith("**/"):
            stripped = pattern[3:]
            parts = filepath.split("/")
            for i in range(len(parts)):
                if fnmatch.fnmatch("/".join(parts[i:]), stripped):
                    return True
            return False
        return fnmatch.fnmatch(filepath, pattern)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        db_paths = self._query_db_filepaths()
        if path:
            db_paths = [p for p in db_paths if p.startswith(path)]

        db_matches: list[dict[str, Any]] = []
        for fp in db_paths:
            if self._glob_match(fp, pattern):
                db_matches.append({"path": fp, "is_dir": False, "size": 0, "modified_at": ""})

        state_result = super().glob(pattern, path)
        state_matches = state_result.matches or []

        seen: set[str] = {m["path"] for m in state_matches}
        to_add = [m for m in db_matches if m["path"] not in seen]

        if to_add:
            merged = sorted(
                state_matches + to_add,
                key=lambda x: x["path"],
            )
            return GlobResult(matches=merged)
        return state_result

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        db_matches = self._query_db_grep(pattern, path=path, glob=glob)

        state_result = super().grep(pattern, path=path, glob=glob)
        state_matches = state_result.matches or []

        seen: set[tuple[str, int]] = {(m["path"], m["line"]) for m in state_matches}
        to_add = [m for m in db_matches if (m["path"], m["line"]) not in seen]

        if to_add:
            merged = sorted(
                state_matches + to_add,
                key=lambda x: (x["path"], x["line"]),
            )
            return GrepResult(matches=merged)
        return state_result
