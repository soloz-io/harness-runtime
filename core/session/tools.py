"""Image-baked CLI tools support for agent sessions.

Mirrors ``SkillsManager`` but for the ``tools/`` folder instead of ``skills/``.
Scans the container image at ``{HARNESS_IMAGE_DIR}/{node-id}/tools/`` plus the
app-wide ``{HARNESS_IMAGE_DIR}/shared/tools/`` and exposes matched nodes via a
``ToolsContext`` consumed by the topology builder.

Agents only *execute* these tools via ``CustomToolMiddleware``; they do not
read or modify the scripts, so no FilesystemBackend routes or symlinks are
created.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ── env vars ────────────────────────────────────────────────────────────

ENV_IMAGE_DIR = "HARNESS_IMAGE_DIR"
TOOLS_SUBDIR = "tools"


# ── public API ──────────────────────────────────────────────────────────


class ToolsError(RuntimeError):
    """Raised when the image-baked agents root cannot be resolved."""


@dataclass(frozen=True)
class ToolSpec:
    """Resolved locations for a single node's CLI tools.

    ``tools_dir`` is the node-specific ``agents/<node-id>/tools/`` folder
    (``None`` when the node has none). ``shared_dir`` is the app-wide
    ``agents/shared/tools/`` folder merged into every node's ``run_tool``
    dispatch (``None`` when absent).
    """

    node_id: str
    tools_dir: Path | None = None
    shared_dir: Path | None = None

    @property
    def search_dirs(self) -> list[Path]:
        """Node-specific dir first, then the app-wide shared dir."""
        dirs: list[Path] = []
        if self.tools_dir is not None:
            dirs.append(self.tools_dir)
        if self.shared_dir is not None:
            dirs.append(self.shared_dir)
        return dirs


@dataclass(frozen=True)
class ToolsContext:
    """Immutable context passed through session → topology → middleware."""

    node_tools: dict[str, ToolSpec] = field(default_factory=dict)
    """Mapping of ``node_id`` → ``ToolSpec`` for every node that has tools."""


def _resolve_image_dir() -> Path:
    """Resolve the agents root directory from env vars (with legacy fallback)."""
    raw = os.environ.get(ENV_IMAGE_DIR)
    if not raw:
        raise ToolsError(
            f"{ENV_IMAGE_DIR} is required but not set. "
            f"Set it to the container path containing agents/<node-id>/tools/."
        )
    path = Path(raw)
    if not path.is_dir():
        raise ToolsError(f"{ENV_IMAGE_DIR}={raw} does not exist on disk.")
    return path


class ToolsManager:
    """Owns the tools lifecycle: discovery and teardown.

    ``initialize()`` scans the image-baked agents root for
    ``agents/<node-id>/tools/`` directories, plus the app-wide
    ``agents/shared/tools/`` directory, and returns a ``ToolsContext``
    that the topology builder threads into ``CustomToolMiddleware``.
    """

    def __init__(self, agent_definition: dict[str, Any]) -> None:
        self._agent_definition = agent_definition

    def initialize(self) -> ToolsContext:
        """Discover nodes that have a tools folder on disk."""
        try:
            image_dir = _resolve_image_dir()
        except ToolsError:
            logger.warning("tools_image_dir_missing")
            return ToolsContext()

        shared_dir = image_dir / "shared" / TOOLS_SUBDIR
        if shared_dir.is_dir():
            logger.info("shared_tools_discovered", path=str(shared_dir))
        else:
            shared_dir = None

        node_tools: dict[str, ToolSpec] = {}
        agent_ids: set[str] = set()

        for node in self._agent_definition.get("nodes", []):
            node_id = node.get("id", "")
            if node_id:
                agent_ids.add(node_id)
            config = node.get("config", {})
            name = config.get("name", "")
            if name:
                agent_ids.add(name)
            for sub in config.get("subagents", []):
                if isinstance(sub, dict):
                    sub_name = sub.get("name", "")
                    if sub_name:
                        agent_ids.add(sub_name)
                    sub_id = sub.get("id", "")
                    if sub_id:
                        agent_ids.add(sub_id)

        for agent_id in agent_ids:
            own_dir = image_dir / agent_id / TOOLS_SUBDIR
            own = own_dir if own_dir.is_dir() else None
            if own is None and shared_dir is None:
                continue
            node_tools[agent_id] = ToolSpec(node_id=agent_id, tools_dir=own, shared_dir=shared_dir)
            logger.info("tools_discovered", node_id=agent_id, path=str(own or shared_dir))

        if not node_tools:
            logger.info("no_nodes_with_tools")

        return ToolsContext(node_tools=node_tools)

    def cleanup(self) -> None:
        """No-op for now; exists for symmetry with SkillsManager."""
