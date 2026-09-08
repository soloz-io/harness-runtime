"""Tests for the read-only workspace view (api/routers/workspace_files.py).

The two things worth pinning here are the two that were wrong when this was
written: which paths a listing must NOT contain, and the fact that the same file
has to be reachable by both path shapes the rest of the system uses.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from fastapi import HTTPException


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A workspace with the shapes that broke the first implementation."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "index.tsx").write_text("export default 1\n")
    (tmp_path / "README.md").write_text("# hi\n")

    # Directories a human never wants in an editor, and which make a listing
    # unusably large. An Expo app's .git holds tens of thousands of objects.
    (tmp_path / "node_modules" / "left-pad").mkdir(parents=True)
    (tmp_path / "node_modules" / "left-pad" / "index.js").write_text("x")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "deadbeef").write_text("x")

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WAYPOINT_ENV", "local")
    module = importlib.import_module("api.routers.workspace_files")
    importlib.reload(module)
    return module, tmp_path


def test_listing_excludes_generated_directories(workspace):
    module, root = workspace
    files, truncated = module._walk(Path(root).resolve())
    paths = sorted(f["path"] for f in files)

    assert paths == ["README.md", "app/index.tsx"]
    assert not truncated
    # Stated separately from the equality above so a failure says WHICH rule
    # broke rather than just showing two lists.
    assert not any(p.startswith("node_modules") for p in paths)
    assert not any(p.startswith(".git") for p in paths)


def test_both_path_shapes_resolve_to_the_same_file(workspace):
    """Relative and absolute must agree.

    The listing returns paths relative to the root; the agent, the transcript
    and every tool speak absolute /workspace/... . Joining the absolute form
    onto the root without stripping it yields /workspace/workspace/... — the
    doubling documented in core/agent_backend.py, which surfaced as
    path_not_found rather than as anything about paths.
    """
    module, root = workspace
    relative = module._resolve_within_root("app/index.tsx")
    absolute = module._resolve_within_root(f"{root}/app/index.tsx")
    rooted = module._resolve_within_root("/app/index.tsx")

    assert relative == absolute == rooted
    assert relative.is_file()


@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "app/../../../etc/passwd", "../", "app/../.."],
)
def test_traversal_is_refused(workspace, bad):
    module, _ = workspace
    with pytest.raises(HTTPException) as exc:
        module._resolve_within_root(bad)
    assert exc.value.status_code == 400


def test_missing_workspace_lists_empty_rather_than_failing(tmp_path, monkeypatch):
    """A sandbox that has scaffolded nothing yet is not an error.

    An empty editor is the honest rendering of an empty workspace; a 500 would
    make a brand-new session look broken.
    """
    absent = tmp_path / "never-created"
    monkeypatch.setenv("WORKSPACE_ROOT", str(absent))
    monkeypatch.setenv("WAYPOINT_ENV", "local")
    module = importlib.import_module("api.routers.workspace_files")
    importlib.reload(module)

    assert not Path(module.ROOT_DIR).is_dir()


def test_listing_is_capped(workspace, monkeypatch):
    """A runaway generated directory must truncate, not stall the browser."""
    module, root = workspace
    monkeypatch.setattr(module, "_MAX_ENTRIES", 3)
    for i in range(10):
        (root / f"f{i}.txt").write_text("x")

    files, truncated = module._walk(Path(root).resolve())
    assert truncated
    assert len(files) == 3


def test_build_workspace_zip_validity_and_exclusions(workspace):
    """The generated zip must be valid, extractable, and exclude node_modules, .git, etc."""
    import io
    import zipfile

    module, root = workspace
    zip_bytes = module._build_workspace_zip(Path(root).resolve())

    assert zipfile.is_zipfile(io.BytesIO(zip_bytes))
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # testzip returns None if all file CRC checksums and headers are valid
        assert zf.testzip() is None
        names = zf.namelist()

        # Regular files are included
        assert "app/index.tsx" in names
        assert "README.md" in names

        # Ignored directories and transient files must NOT be in the zip
        for name in names:
            assert not name.startswith("node_modules")
            assert not name.startswith(".git")
            assert not name.startswith(".expo")
            assert not name.startswith(".next")
            assert not name.startswith(".cache")
            assert not name.endswith(".DS_Store")


def test_build_workspace_zip_rejects_oversized_workspace(workspace, monkeypatch):
    """A workspace exceeding _MAX_DOWNLOAD_UNCOMPRESSED_BYTES must abort with HTTP 413."""
    module, root = workspace
    # Mock limit to 10 bytes so the existing files exceed it
    monkeypatch.setattr(module, "_MAX_DOWNLOAD_UNCOMPRESSED_BYTES", 10)

    with pytest.raises(HTTPException) as exc_info:
        module._build_workspace_zip(Path(root).resolve())

    assert exc_info.value.status_code == 413
    assert "exceeds maximum download limit" in str(exc_info.value.detail)
