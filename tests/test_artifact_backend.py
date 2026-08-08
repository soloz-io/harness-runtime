"""Regression tests for SessionArtifactBackend read/edit priority.

The backend must serve the current session's channel state (read-your-writes)
before falling back to the ``agent_output_files`` DB scope.  A prior bug had
``read()`` query the DB first, so a subagent writing a file and immediately
re-reading it saw the stale pre-write DB snapshot.  ``edit()`` must likewise
hydrate DB-backed files into state before performing the replacement.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.backends.artifact import SessionArtifactBackend


class _FakePool:
    """ConnectionPool stand-in — never reached when _query_db is mocked."""

    def connection(self) -> Any:  # pragma: no cover - defensive
        raise AssertionError("pool.connection() should not be called")


def _backend(state: dict[str, Any] | None = None) -> SessionArtifactBackend:
    backend = SessionArtifactBackend(
        workspace_id="ws-1",
        session_id="sess-1",
        pool=_FakePool(),  # type: ignore[arg-type]
        app_id="app-1",
    )
    backend._state = state if state is not None else {}  # type: ignore[attr-defined]
    return backend


def _patch_state(backend: SessionArtifactBackend) -> None:
    """Simulate the LangGraph files channel via _read_files/_send_files_update."""

    def _read_files() -> dict[str, Any]:
        return backend._state  # type: ignore[attr-defined]

    def _send_files_update(update: dict[str, Any]) -> None:
        backend._state.update(update)  # type: ignore[attr-defined]

    backend._read_files = _read_files  # type: ignore[method-assign]
    backend._send_files_update = _send_files_update  # type: ignore[method-assign]


def test_read_returns_state_when_present_without_touching_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend({"/workspace/.global/brand-brief.md": {"content": "FRESH"}})
    _patch_state(backend)
    queried: list[str] = []

    def fake_query_db(path: str) -> Any:
        queried.append(path)
        return None

    monkeypatch.setattr(backend, "_query_db", fake_query_db)

    result = backend.read("/workspace/.global/brand-brief.md")

    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["content"] == "FRESH"
    assert queried == [], "DB must not be queried when the file is in state"


def test_read_falls_back_to_db_when_absent_from_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend({})
    _patch_state(backend)

    def fake_query_db(path: str) -> Any:
        assert path == "/workspace/.global/brand-brief.md"
        return ("DB CONTENT",)

    monkeypatch.setattr(backend, "_query_db", fake_query_db)

    result = backend.read("/workspace/.global/brand-brief.md")

    assert result.error is None
    assert result.file_data is not None
    assert result.file_data["content"] == "DB CONTENT"


def test_read_returns_error_when_missing_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend({})
    _patch_state(backend)
    monkeypatch.setattr(backend, "_query_db", lambda path: None)

    result = backend.read("/workspace/nope.md")

    assert result.error is not None
    assert "/workspace/nope.md" in result.error


def test_edit_hydrates_db_file_into_state_then_edits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend({})
    _patch_state(backend)

    def fake_query_db(path: str) -> Any:
        assert path == "/workspace/.global/brand-brief.md"
        return ("line1\ncontract_hash: old\ngenerated_by: brand-discovery-agent",)

    monkeypatch.setattr(backend, "_query_db", fake_query_db)

    result = backend.edit(
        "/workspace/.global/brand-brief.md",
        "contract_hash: old",
        "contract_hash: 855fc8f15a505f9090461b0d9f647060",
    )

    assert result.error is None
    assert result.occurrences == 1
    state_content = backend._state[  # type: ignore[attr-defined]
        "/workspace/.global/brand-brief.md"
    ]["content"]
    assert "contract_hash: 855fc8f15a505f9090461b0d9f647060" in state_content


def test_edit_hydration_marks_modified_and_keeps_created_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _backend({})
    _patch_state(backend)
    monkeypatch.setattr(
        backend,
        "_query_db",
        lambda path: ("a\nb",),
    )

    backend.edit("/workspace/f.md", "a", "x")

    hydrated = backend._state["/workspace/f.md"]  # type: ignore[attr-defined]
    assert hydrated["content"] == "x\nb"
    assert hydrated.get("created_at")
    assert hydrated.get("modified_at")
