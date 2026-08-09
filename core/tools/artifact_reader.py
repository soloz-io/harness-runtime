"""Secure DB-backed artifact reader for agent CLI tools.

Agent tool scripts (``workdir.py``) call :func:`read_artifact_from_db` to
fetch file content written by the agent (via ``write_file``) directly from the
``agent_output_files`` table.  This module lives inside the installed
``harness-runtime`` package — outside the agent's ``/workspace`` folder — so
agent scripts contain **zero SQL, zero credentials, and zero connection
strings**, and agents cannot edit or corrupt the query at runtime.

Scope semantics mirror ``core.message_writer.write_agent_output_files``
(``agent_output_files.session_id`` is a *scope key*, not necessarily the
executing session):

- **Builder sessions** (``workspace_id == app_id``): every file is keyed by
  the app id, so the whole app's artifacts are shared across sessions.
- **Playground sessions**: files under ``.global/`` are keyed by the app id
  (app-wide artifacts); everything else is keyed by the session id.

Stored ``filepath`` values are normalized relative to the workspace root
(e.g. ``/workspace/audio.md`` → ``audio.md``, ``.global/brand-brief.md``
keeps its prefix), matching ``write_agent_output_files``.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import psycopg

DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 2.0


def _normalize_filepath(file_path: str) -> str:
    """Normalize an agent path to its stored DB form (mirrors message_writer)."""
    filepath = file_path.lstrip("/")
    if filepath.startswith("home/ubuntu/"):
        filepath = filepath[12:]
    elif filepath.startswith("workspace/"):
        filepath = filepath[10:]
    return filepath


def _scope_key(filepath: str, session_id: str, workspace_id: str, app_id: Optional[str]) -> str:
    """Mirror ``core.message_writer._file_scope_key``."""
    if app_id and workspace_id and workspace_id == app_id:
        return app_id
    if app_id and filepath.startswith(".global/"):
        return app_id
    return session_id


def read_artifact_from_db(
    filename: str,
    *,
    session_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    app_id: Optional[str] = None,
    retries: int = DEFAULT_RETRIES,
    delay: float = DEFAULT_RETRY_DELAY,
) -> str:
    """Read an artifact's content from ``agent_output_files``.

    Identifiers fall back to the environment (``SESSION_ID``,
    ``WORKSPACE_ID``, ``APP_ID``) which the ``run_tool`` middleware injects
    into the CLI subprocess.  A bounded retry loop tolerates the small
    async window between an agent ``write_file`` and its DB projection.

    Args:
        filename: Agent path, e.g. ``/workspace/audio.md`` or ``audio.md``.
        session_id: Session id (defaults to ``$SESSION_ID``).
        workspace_id: Workspace id (defaults to ``$WORKSPACE_ID``).
        app_id: App id (defaults to ``$APP_ID``).
        retries: Number of query attempts before raising.
        delay: Seconds to sleep between attempts.

    Returns:
        The file content as a string.

    Raises:
        ValueError: If ``DATABASE_URL``/``SESSION_ID`` are unavailable.
        FileNotFoundError: If the artifact is not found after *retries*.
    """
    db_url = os.environ.get("DATABASE_URL")
    sid = session_id or os.environ.get("SESSION_ID")
    wid = workspace_id or os.environ.get("WORKSPACE_ID", "")
    aid = app_id or os.environ.get("APP_ID")
    if not db_url or not sid:
        raise ValueError("DATABASE_URL or SESSION_ID not available")

    filepath = _normalize_filepath(filename)
    scope = _scope_key(filepath, sid, wid, aid)

    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with psycopg.connect(db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT content FROM agent_output_files
                        WHERE session_id = %s AND filepath = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (scope, filepath),
                    )
                    row = cur.fetchone()
                    if row is not None and row[0] is not None:
                        return str(row[0])
        except Exception as e:  # noqa: BLE001
            last_error = e
        if attempt < retries:
            time.sleep(delay)

    detail = f" (last query error: {last_error})" if last_error else ""
    raise FileNotFoundError(
        f"Artifact '{filename}' not found in DB (scope={scope}, filepath={filepath}){detail}"
    )
