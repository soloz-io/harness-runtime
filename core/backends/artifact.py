"""SessionArtifactBackend: StateBackend + DB-backed cross-session file operations.

Extends deepagents StateBackend to surface files from other sessions in the
same scope, plus app-scoped ``.global/`` artifacts.

Two namespaces are served by one backend (both attached whenever a session
request carries both ``session_id`` and ``app_id``):

- **Session workspace** (``/workspace/...``): files keyed by ``workspace_id``
  in ``agent_output_files``.  Builder sessions scope the workspace to the app
  (``workspace_id == app_id``) so the whole app's files are visible; playground
  sessions scope to the session id (per-session isolation, ADR-014).
- **App globals** (``/workspace/.global/...``): files keyed by ``app_id`` with
  a ``.global/`` path prefix — app-wide artifacts shared across sessions of
  the same app.  Separate namespace: normal-path reads never see them.

Writes always continue through the inherited StateBackend channel path — the
values-event → ``message_writer.py`` projection decides the storage key from
the ``.global/`` prefix and the session's scopes.
"""

from __future__ import annotations

import fnmatch
from typing import Any, LiteralString, Optional, cast

import structlog
from psycopg import sql

try:
    from deepagents.backends.protocol import (
        EditResult,
        FileData,
        FileInfo,
        GlobResult,
        GrepMatch,
        GrepResult,
        LsResult,
        ReadResult,
    )
    from deepagents.backends.state import StateBackend
    from deepagents.backends.utils import create_file_data
except ImportError:
    raise ImportError(
        "deepagents package is required. Install it with: pip install deepagents>=0.2.0"
    ) from None

try:
    from psycopg_pool import ConnectionPool
except ImportError:
    ConnectionPool: Any = None  # type: ignore[assignment]

logger = structlog.get_logger(__name__)

GLOBAL_PREFIX = ".global/"


def _file_info(path: str, is_dir: bool = False) -> FileInfo:
    return FileInfo(path=path, is_dir=is_dir, size=0, modified_at="")


class SessionArtifactBackend(StateBackend):
    """StateBackend that also surfaces DB-backed files across the workspace.

    Reads:
        State-first with a DB fallback.  ``read()`` serves the current
        session's channel files (freshest source — read-your-writes) and only
        queries ``agent_output_files`` for files absent from the channel, e.g.
        a cross-session or app-global first read.  ``.global/`` paths are read
        from the app-scoped rows (``session_id = app_id``).

    Writes / upload_files:
        All inherited from ``StateBackend`` unchanged — they write to the
        LangGraph ``state["files"]`` channel, which triggers the existing
        root-values-event → ``message_writer.py`` → ``agent_output_files`` DB
        projection path.

    Edit:
        Overridden — hydrates a DB-backed file into the state channel before
        delegating, so app-global/cross-session files can be edited even before
        they are materialized in the active session.

    Grep / glob / ls:
        Merge DB results from the scope with the current session's channel
        results, deduplicated by file path.
    """

    def __init__(
        self,
        workspace_id: str,
        session_id: str,
        pool: ConnectionPool,
        app_id: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.workspace_id = workspace_id
        self.session_id = session_id
        self.app_id = app_id
        self._pool = pool

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_global_db_path(db_path: str) -> bool:
        return db_path == ".global" or db_path.startswith(GLOBAL_PREFIX)

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

    def _scope_condition(self) -> tuple[LiteralString, list[Any]]:
        """WHERE condition covering the session workspace + app globals.

        Session workspace: rows keyed by ``workspace_id`` (excluding the
        current session's channel files, which StateBackend already serves)
        and never the ``.global/`` namespace.  App globals: rows keyed by
        ``app_id`` under the ``.global/`` prefix.
        """
        clauses = ["(session_id = %s AND session_id != %s AND filepath NOT LIKE '.global/%%')"]
        params: list[Any] = [self.workspace_id, self.session_id]
        if self.app_id:
            clauses.append("(session_id = %s AND filepath LIKE '.global/%%')")
            params.append(self.app_id)
        return cast(LiteralString, "(" + " OR ".join(clauses) + ")"), params

    def _query_db(self, path: str) -> tuple | None:
        """Return the most recent file content for *path* across the scope."""
        db_path = self._normalize_db_path(path)
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    if self._is_global_db_path(db_path):
                        if not self.app_id:
                            return None
                        cur.execute(
                            """
                            SELECT aof.content
                            FROM agent_output_files aof
                            WHERE aof.session_id = %s
                              AND aof.filepath = %s
                            ORDER BY aof.created_at DESC
                            LIMIT 1
                            """,
                            (self.app_id, db_path),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT aof.content
                            FROM agent_output_files aof
                            WHERE aof.session_id = %s
                              AND aof.session_id != %s
                              AND aof.filepath = %s
                              AND aof.filepath NOT LIKE '.global/%%'
                            ORDER BY aof.created_at DESC
                            LIMIT 1
                            """,
                            (self.workspace_id, self.session_id, db_path),
                        )
                    return cur.fetchone()
        except Exception:
            logger.exception("session_artifact_backend_db_read_failed", path=path)
            return None

    def _query_db_ls(self, path: str) -> list[FileInfo]:
        """Return distinct file entries under *path* from the DB scope.

        Mirrors ``StateBackend.ls`` semantics: files directly in the
        directory are returned as entries, while nested paths are collapsed
        into their immediate subdirectory (trailing ``/``, ``is_dir=True``).
        """
        db_path = self._normalize_db_path(path)
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    if self._is_global_db_path(db_path):
                        if not self.app_id:
                            return []
                        prefix = db_path if db_path.endswith("/") else db_path + "/"
                        cur.execute(
                            """
                            SELECT DISTINCT aof.filepath
                            FROM agent_output_files aof
                            WHERE aof.session_id = %s
                              AND aof.filepath LIKE %s
                            """,
                            (self.app_id, prefix + "%"),
                        )
                    else:
                        scope_where, params = self._scope_condition()
                        prefix = db_path if db_path.endswith("/") or not db_path else db_path + "/"
                        cur.execute(
                            sql.SQL(
                                """
                                SELECT DISTINCT aof.filepath
                                FROM agent_output_files aof
                                WHERE {}
                                  AND aof.filepath LIKE %s
                                """
                            ).format(sql.SQL(scope_where)),
                            params + [prefix + "%"],
                        )
                    rows = cur.fetchall()
        except Exception:
            logger.exception("session_artifact_backend_db_ls_failed", path=path)
            return []

        entries: list[FileInfo] = []
        subdirs: set[str] = set()
        for (row,) in rows:
            relative = row[len(prefix) :]
            if "/" in relative:
                subdirs.add(prefix + relative.split("/", 1)[0] + "/")
            else:
                entries.append(_file_info(self._format_agent_path(row)))
        for subdir in sorted(subdirs):
            entries.append(_file_info(self._format_agent_path(subdir), is_dir=True))
        return entries

    def _query_db_filepaths(self) -> list[str]:
        """Return all distinct filepaths in the scope (for glob)."""
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    scope_where, params = self._scope_condition()
                    cur.execute(
                        sql.SQL(
                            """
                            SELECT DISTINCT aof.filepath
                            FROM agent_output_files aof
                            WHERE {}
                            """
                        ).format(sql.SQL(scope_where)),
                        params,
                    )
                    return [self._format_agent_path(row[0]) for row in cur.fetchall()]
        except Exception:
            logger.exception("session_artifact_backend_db_filepaths_failed")
            return []

    def _query_db_grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch]:
        """Search file contents from the scope for a literal substring."""
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    scope_where, params = self._scope_condition()
                    cur.execute(
                        sql.SQL(
                            """
                            SELECT DISTINCT ON (aof.filepath) aof.filepath, aof.content
                            FROM agent_output_files aof
                            WHERE {}
                            ORDER BY aof.filepath, aof.created_at DESC
                            """
                        ).format(sql.SQL(scope_where)),
                        params,
                    )
                    rows = cur.fetchall()
        except Exception:
            logger.exception("session_artifact_backend_db_grep_failed", pattern=pattern)
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
        # Read-your-writes: the current session's channel state is the freshest
        # source.  Only fall back to the DB scope when the file isn't in the
        # active session (e.g. a cross-session or app-global first read).
        state_result = super().read(file_path, offset=offset, limit=limit)
        if state_result.error is None:
            return state_result
        row = self._query_db(file_path)
        if row:
            return ReadResult(
                file_data=FileData(content=row[0], encoding="utf-8"),
            )
        return state_result

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        """Edit a file, hydrating DB-backed files into state first.

        The inherited ``StateBackend.edit`` only operates on the current
        session's channel state.  For files that exist in the DB scope (e.g. an
        app-global ``.global/`` artifact or a cross-session file) but have not
        been materialized into the active session's channel yet, hydrate the DB
        content into state so the string replacement can proceed, then delegate.
        """
        files = self._read_files()
        if file_path not in files:
            row = self._query_db(file_path)
            if row:
                self._send_files_update(
                    {file_path: self._prepare_for_storage(create_file_data(row[0]))}
                )
        return super().edit(file_path, old_string, new_string, replace_all=replace_all)

    def ls(self, path: str) -> LsResult:
        state_result = super().ls(path)
        state_entries = state_result.entries or []
        db_entries = self._query_db_ls(path)

        seen: set[str] = {e["path"] for e in state_entries}
        to_add = [e for e in db_entries if e["path"] not in seen]

        entries = state_entries + to_add

        # Surface the app-globals namespace as a directory when listing the
        # workspace root so agents can discover /workspace/.global/.
        if self.app_id and path in ("/workspace", "/workspace/"):
            if not any(e["path"].rstrip("/") == "/workspace/.global" for e in entries):
                entries = entries + [_file_info("/workspace/.global", is_dir=True)]

        if to_add or (self.app_id and path in ("/workspace", "/workspace/")):
            return LsResult(
                entries=sorted(entries, key=lambda x: x["path"]),
            )
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

        db_matches: list[FileInfo] = []
        for fp in db_paths:
            if self._glob_match(fp, pattern):
                db_matches.append(FileInfo(path=fp, is_dir=False, size=0, modified_at=""))

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
