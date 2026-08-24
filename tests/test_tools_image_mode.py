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
    mw = CustomToolMiddleware([tools_dir])

    run_tool = mw.tools[0]
    result = run_tool.invoke({"tool_name": "../../../etc/passwd", "cli_args": ""})
    assert result["success"] is False


def test_run_tool_rejects_unknown_tool(env_setup: None, image_tools_dir: Path) -> None:
    from core.middleware.custom_tool_middleware import CustomToolMiddleware

    tools_dir = image_tools_dir / "motion-graphics" / "tools"
    mw = CustomToolMiddleware([tools_dir])

    run_tool = mw.tools[0]
    result = run_tool.invoke({"tool_name": "nonexistent_tool", "cli_args": ""})
    assert result["success"] is False


def test_run_tool_lists_available_tools(env_setup: None, image_tools_dir: Path) -> None:
    from core.middleware.custom_tool_middleware import CustomToolMiddleware

    tools_dir = image_tools_dir / "motion-graphics" / "tools"
    mw = CustomToolMiddleware([tools_dir])

    run_tool = mw.tools[0]
    result = run_tool.invoke({"tool_name": "nonexistent_tool", "cli_args": ""})
    assert "seg_cli" in result["output"]
    assert "clips_cli" in result["output"]


# ── shared/tools/ discovery + resolution ────────────────────────────────────


def test_tools_manager_merges_shared_tools_into_every_node(
    monkeypatch: pytest.MonkeyPatch, image_tools_dir: Path
) -> None:
    (image_tools_dir / "shared" / "tools").mkdir(parents=True)
    (image_tools_dir / "shared" / "tools" / "compile_schema_cli.py").write_text("print('ok')")

    monkeypatch.setenv(ENV_IMAGE_DIR, str(image_tools_dir))
    # "bare-node" has no own tools/ dir but must still get shared tools.
    manager = ToolsManager(_definition(["motion-graphics", "bare-node"]))
    ctx = manager.initialize()

    assert "motion-graphics" in ctx.node_tools
    assert "bare-node" in ctx.node_tools
    spec = ctx.node_tools["bare-node"]
    assert spec.tools_dir is None
    assert spec.shared_dir == image_tools_dir / "shared" / "tools"
    assert spec.search_dirs == [image_tools_dir / "shared" / "tools"]
    # Node-specific dir takes precedence over the shared dir.
    mg = ctx.node_tools["motion-graphics"]
    assert mg.search_dirs == [
        image_tools_dir / "motion-graphics" / "tools",
        image_tools_dir / "shared" / "tools",
    ]


def test_run_tool_resolves_shared_tool(
    monkeypatch: pytest.MonkeyPatch, image_tools_dir: Path
) -> None:
    from core.middleware.custom_tool_middleware import CustomToolMiddleware

    shared_dir = image_tools_dir / "shared" / "tools"
    shared_dir.mkdir(parents=True)
    (shared_dir / "compile_schema_cli.py").write_text("print('compiled')")

    mw = CustomToolMiddleware([shared_dir])
    run_tool = mw.tools[0]
    result = run_tool.invoke({"tool_name": "compile_schema_cli", "cli_args": ""})
    assert result["success"] is True
    assert "compiled" in result["output"]


def test_run_tool_prefers_node_dir_over_shared(
    monkeypatch: pytest.MonkeyPatch, image_tools_dir: Path
) -> None:
    from core.middleware.custom_tool_middleware import CustomToolMiddleware

    node_dir = image_tools_dir / "motion-graphics" / "tools"
    shared_dir = image_tools_dir / "shared" / "tools"
    shared_dir.mkdir(parents=True)
    (node_dir / "seg_cli.py").write_text("print('node-seg')")
    (shared_dir / "seg_cli.py").write_text("print('shared-seg')")

    mw = CustomToolMiddleware([node_dir, shared_dir])
    run_tool = mw.tools[0]
    result = run_tool.invoke({"tool_name": "seg_cli", "cli_args": ""})
    assert result["success"] is True
    assert "node-seg" in result["output"]


def test_tools_manager_discovers_nested_subagent_tools(
    monkeypatch: pytest.MonkeyPatch, image_tools_dir: Path
) -> None:
    monkeypatch.setenv(ENV_IMAGE_DIR, str(image_tools_dir))

    nested_def = {
        "name": "test-nested-tools",
        "topology": "composite",
        "nodes": [
            {
                "id": "orchestrator",
                "type": "Orchestrator",
                "config": {"name": "orchestrator"},
            },
            {
                "id": "media-generator",
                "type": "Specialist",
                "config": {
                    "name": "media-generator-agent",
                    "runtime": "deepagent",
                    "subagents": [
                        {
                            "name": "motion-graphics",
                            "tools": ["read_file", "run_tool"],
                        }
                    ],
                },
            },
        ],
    }

    manager = ToolsManager(nested_def)
    ctx = manager.initialize()

    assert "motion-graphics" in ctx.node_tools
    assert ctx.node_tools["motion-graphics"].tools_dir == (
        image_tools_dir / "motion-graphics" / "tools"
    )
