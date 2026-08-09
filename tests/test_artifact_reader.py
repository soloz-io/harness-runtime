"""Unit tests for core.tools.artifact_reader.

Covers scope-key resolution (mirroring ``message_writer._file_scope_key``),
path normalization, retry-until-found behaviour, and env-var fallbacks.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.tools.artifact_reader import (
    read_artifact_from_db,
)

from core.tools import artifact_reader


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = list(rows)
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...]) -> None:
        self.calls.append((query, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._rows:
            return self._rows.pop(0)
        return None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self._cursor


def _patch_connect(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[Any, ...]]) -> _FakeCursor:
    cursor = _FakeCursor(rows)

    def fake_connect(db_url: str) -> _FakeConn:  # noqa: ARG001
        return _FakeConn(cursor)

    monkeypatch.setattr(artifact_reader.psycopg, "connect", fake_connect)
    return cursor


def _patch_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str = "sess-1",
    workspace_id: str | None = None,
    app_id: str | None = None,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("SESSION_ID", session_id)
    if workspace_id is None:
        monkeypatch.delenv("WORKSPACE_ID", raising=False)
    else:
        monkeypatch.setenv("WORKSPACE_ID", workspace_id)
    if app_id is None:
        monkeypatch.delenv("APP_ID", raising=False)
    else:
        monkeypatch.setenv("APP_ID", app_id)


def test_reads_normalized_session_scoped_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playground (workspace != app) non-global file is keyed by session_id."""
    _patch_env(monkeypatch, workspace_id="ws-1", app_id="app-1")
    cursor = _patch_connect(monkeypatch, [("NORMALIZED SCRIPT",)])

    content = read_artifact_from_db("/workspace/audio.md")

    assert content == "NORMALIZED SCRIPT"
    query, params = cursor.calls[0]
    assert "session_id = %s" in query
    assert params == ("sess-1", "audio.md")


def test_global_file_keyed_by_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_env(monkeypatch, workspace_id="ws-1", app_id="app-1")
    cursor = _patch_connect(monkeypatch, [("BRAND BRIEF",)])

    content = read_artifact_from_db("/workspace/.global/brand-brief.md")

    assert content == "BRAND BRIEF"
    _, params = cursor.calls[0]
    assert params == ("app-1", ".global/brand-brief.md")


def test_builder_session_keyed_by_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Builder sessions (workspace_id == app_id) key every file by app_id."""
    _patch_env(monkeypatch, workspace_id="app-1", app_id="app-1")
    cursor = _patch_connect(monkeypatch, [("APP ARTIFACT",)])

    content = read_artifact_from_db("audio.md")

    assert content == "APP ARTIFACT"
    _, params = cursor.calls[0]
    assert params == ("app-1", "audio.md")


def test_explicit_args_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_env(monkeypatch, session_id="env-sess", workspace_id="env-ws", app_id="env-app")
    cursor = _patch_connect(monkeypatch, [("X",)])

    content = read_artifact_from_db(
        "audio.md",
        session_id="arg-sess",
        workspace_id="arg-ws",
        app_id="arg-app",
    )

    assert content == "X"
    _, params = cursor.calls[0]
    assert params == ("arg-sess", "audio.md")


def test_retries_until_row_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    """First attempts return no row; a later attempt finds it."""
    _patch_env(monkeypatch)
    cursor = _FakeCursor([None, None, ("FOUND",)])

    def fake_connect(db_url: str) -> _FakeConn:  # noqa: ARG001
        return _FakeConn(cursor)

    monkeypatch.setattr(artifact_reader.psycopg, "connect", fake_connect)
    monkeypatch.setattr(artifact_reader.time, "sleep", lambda s: None)

    content = read_artifact_from_db("audio.md", retries=3, delay=0.01)

    assert content == "FOUND"
    assert len(cursor.calls) == 3


def test_raises_file_not_found_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_env(monkeypatch)
    cursor = _patch_connect(monkeypatch, [None, None])

    with pytest.raises(FileNotFoundError, match="audio.md"):
        read_artifact_from_db("audio.md", retries=2, delay=0.01)

    assert len(cursor.calls) == 2


def test_raises_value_error_without_db_or_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SESSION_ID", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL or SESSION_ID"):
        read_artifact_from_db("audio.md")
