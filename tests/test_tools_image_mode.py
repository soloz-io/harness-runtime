"""Unit tests for image-baked CLI tools support.

Covers:
  - ToolsManager discovering ``agents/<node-id>/tools/`` from ``HARNESS_IMAGE_DIR``.
  - Graceful no-op when image dir is absent or unset.
  - ``CustomToolMiddleware`` validation (no path traversal, unknown tool errors).

Does NOT require PostgreSQL, Redis, an HTTP server, or git network access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.session.tools import (
    ENV_IMAGE_DIR,
    ToolSpec,
    ToolsContext,
    ToolsError,
    ToolsManager,
)


def _definition(node_ids: list[str]) -> dict:
    return {
        "name": "test-agents",
        "topology": "agent-dag",
        "nodes": [
            {
                "id": nid,
                "type": "Specialist",
                "config": {"name": nid},
            }
            for nid in node_ids
        ],
    }


@pytest.fixture()
def image_tools_dir(tmp_path: Path) -> Path:
    """A fake image-baked agents root: ``agents/<node-id>/tools/<script>.py``."""
    agents_dir = tmp_path / "image-agents"
    for node_id in ("motion-graphics", "code-writer"):
        tools_dir = agents_dir / node_id / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "seg_cli.py").write_text("print('seg')")
        (tools_dir / "clips_cli.py").write_text("print('clips')")
    return agents_dir


@pytest.fixture()
def env_setup(monkeypatch: pytest.MonkeyPatch, image_tools_dir: Path) -> None:
    monkeypatch.setenv(ENV_IMAGE_DIR, str(image_tools_dir))


# ── ToolsManager tests ────────────────────────────────────────────────────


def test_tools_manager_discovers_nodes_with_tools(env_setup: None, image_tools_dir: Path) -> None:
    manager = ToolsManager(_definition(["motion-graphics", "code-writer"]))
    ctx = manager.initialize()

    assert len(ctx.node_tools) == 2
    assert "motion-graphics" in ctx.node_tools
    assert "code-writer" in ctx.node_tools
    assert ctx.node_tools["motion-graphics"].tools_dir == (
        image_tools_dir / "motion-graphics" / "tools"
    )


def test_tools_manager_skips_nodes_without_tools(
    monkeypatch: pytest.MonkeyPatch, image_tools_dir: Path
) -> None:
    monkeypatch.setenv(ENV_IMAGE_DIR, str(image_tools_dir))
    manager = ToolsManager(_definition(["motion-graphics", "no-tools-here"]))
    ctx = manager.initialize()

    assert "motion-graphics" in ctx.node_tools
    assert "no-tools-here" not in ctx.node_tools


def test_tools_manager_no_nodes_with_tools(
    monkeypatch: pytest.MonkeyPatch, image_tools_dir: Path
) -> None:
    monkeypatch.setenv(ENV_IMAGE_DIR, str(image_tools_dir))
    manager = ToolsManager(_definition(["bare-node"]))
    ctx = manager.initialize()

    assert ctx.node_tools == {}


def test_tools_manager_image_dir_missing_returns_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_IMAGE_DIR, str(tmp_path / "nonexistent"))
    manager = ToolsManager(_definition(["motion-graphics"]))
    ctx = manager.initialize()

    # Should not raise — just returns empty context
    assert ctx.node_tools == {}


def test_tools_manager_image_dir_unset_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_IMAGE_DIR, raising=False)
    manager = ToolsManager(_definition(["motion-graphics"]))
    ctx = manager.initialize()

    assert ctx.node_tools == {}


def test_tools_context_is_empty_when_no_tools() -> None:
    ctx = ToolsContext()
    assert ctx.node_tools == {}


# ── CustomToolMiddleware tests ──────────────────────────────────────────────


def test_run_tool_rejects_path_traversal(env_setup: None, image_tools_dir: Path) -> None:
    from core.middleware.custom_tool_middleware import CustomToolMiddleware

    tools_dir = image_tools_dir / "motion-graphics" / "tools"
    mw = CustomToolMiddleware(tools_dir)

    run_tool = mw.tools[0]
    result = run_tool.invoke({"tool_name": "../../../etc/passwd", "cli_args": ""})
    assert result["success"] is False


def test_run_tool_rejects_unknown_tool(env_setup: None, image_tools_dir: Path) -> None:
    from core.middleware.custom_tool_middleware import CustomToolMiddleware

    tools_dir = image_tools_dir / "motion-graphics" / "tools"
    mw = CustomToolMiddleware(tools_dir)

    run_tool = mw.tools[0]
    result = run_tool.invoke({"tool_name": "nonexistent_tool", "cli_args": ""})
    assert result["success"] is False


def test_run_tool_lists_available_tools(env_setup: None, image_tools_dir: Path) -> None:
    from core.middleware.custom_tool_middleware import CustomToolMiddleware

    tools_dir = image_tools_dir / "motion-graphics" / "tools"
    mw = CustomToolMiddleware(tools_dir)

    run_tool = mw.tools[0]
    result = run_tool.invoke({"tool_name": "nonexistent_tool", "cli_args": ""})
    assert "seg_cli" in result["output"]
    assert "clips_cli" in result["output"]
