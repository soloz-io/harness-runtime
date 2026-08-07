"""Image-baked CLI tools support for agent sessions.

Mirrors ``SkillsManager`` but for the ``tools/`` folder instead of ``skills/``.
Scans the container image at ``{HARNESS_IMAGE_DIR}/{node-id}/tools/`` and
exposes matched nodes via a ``ToolsContext`` consumed by the topology builder.

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
    """Resolved location + allow-list for a single node's CLI tools."""

    node_id: str
    tools_dir: Path  # absolute path in the container image


@dataclass(frozen=True)
class ToolsContext:
    """Immutable context passed through session → topology → middleware."""

    node_tools: dict[str, ToolSpec] = field(default_factory=dict)
    """Mapping of ``node_id`` → ``ToolSpec`` for every node that has a tools folder."""


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
    ``agents/<node-id>/tools/`` directories and returns a ``ToolsContext``
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

        node_tools: dict[str, ToolSpec] = {}
        for node in self._agent_definition.get("nodes", []):
            node_id = node.get("id", "")
            if not node_id:
                continue
            candidate = image_dir / node_id / TOOLS_SUBDIR
            if candidate.is_dir():
                node_tools[node_id] = ToolSpec(node_id=node_id, tools_dir=candidate)
                logger.info("tools_discovered", node_id=node_id, path=str(candidate))

        if not node_tools:
            logger.info("no_nodes_with_tools")

        return ToolsContext(node_tools=node_tools)

    def cleanup(self) -> None:
        """No-op for now; exists for symmetry with SkillsManager."""
