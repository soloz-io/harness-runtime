import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

# Environment variable pointing at the agents root baked into the container
# image. SkillsManager resolves per-node skills from
# ``{HARNESS_IMAGE_DIR}/{node-id}/skills/``. Required — there is no
# git-clone fallback.
ENV_IMAGE_DIR = "HARNESS_IMAGE_DIR"

# Runtime root where skills are exposed via FilesystemBackend routes and
# symlinks. Overridable so the sandbox can be exercised outside the container
# (where /workspace is not writable); default matches the production layout.
ENV_SKILLS_RUNTIME_BASE = "HARNESS_SKILLS_RUNTIME_BASE"
DEFAULT_SKILLS_RUNTIME_BASE = "/workspace/.builder"


def _skills_runtime_base() -> Path:
    return Path(os.environ.get(ENV_SKILLS_RUNTIME_BASE, DEFAULT_SKILLS_RUNTIME_BASE))


class SkillsError(RuntimeError):
    """Raised when skills cannot be sourced from the container image.

    There is no fallback: ``HARNESS_IMAGE_DIR`` must be set to an
    existing agents root inside the image, or sessions fail fast.
    """


@dataclass(frozen=True)
class SkillsContext:
    composite_backend: Optional[Any] = None
    skill_router: Optional[Any] = None


class SkillsManager:
    """Owns the full skills lifecycle: setup and teardown.

    Skills are sourced exclusively from the container image. ``initialize()``
    resolves the agents root from ``HARNESS_IMAGE_DIR`` (a hard error
    when unset or missing on disk), copies each node's skills from
    ``agents/<node-id>/skills/`` into per-skill temporary directories, wires
    FilesystemBackend routes, builds a CompositeBackend, and creates stable
    filesystem symlinks. ``cleanup()`` tears everything down.
    """

    def __init__(
        self,
        agent_definition: dict[str, Any],
        artifact_backend: Any,
    ) -> None:
        self._agent_definition = agent_definition
        self._artifact_backend = artifact_backend
        self._tmp_dirs: dict[str, Path] = {}
        self._scratch_dir: Optional[str] = None
        self._router: Optional[Any] = None

    def initialize(self) -> SkillsContext:
        """Set up skills from the image-baked agents root."""
        skills_paths = self._collect_skills()
        if not skills_paths:
            logger.info("no_skills_defined_in_agent_definition")
            return SkillsContext()

        agents_path = self._resolve_agents_dir()
        self._tmp_dirs = self._isolate_skills(agents_path)

        skill_routes = self._build_filesystem_routes()
        self._create_skill_symlinks()
        self._create_scratch(skill_routes)

        # Build per-agent skill wrapper routes
        from core.skill_router import AgentSkillRouter

        self._router = AgentSkillRouter(self._agent_definition, self._tmp_dirs)
        skill_routes.update(self._router.build_routes())

        composite_backend = self._build_composite_backend(skill_routes)
        return SkillsContext(composite_backend=composite_backend, skill_router=self._router)

    # ── internal helpers ──────────────────────────────────────────────

    def _collect_skills(self) -> list[str]:
        skills: list[str] = []
        for node in self._agent_definition.get("nodes", []):
            node_skills = node.get("config", {}).get("skills", [])
            skills.extend(node_skills)
        return skills

    def _resolve_agents_dir(self) -> Path:
        """Resolve the image-baked agents root — hard error if unavailable."""
        image_dir = os.environ.get(ENV_IMAGE_DIR)
        if not image_dir:
            raise SkillsError(
                f"{ENV_IMAGE_DIR} is required. "
                "All skills must be baked into the harness Docker image."
            )
        path = Path(image_dir)
        if not path.is_dir():
            raise SkillsError(f"{ENV_IMAGE_DIR}={image_dir} does not exist in the container.")
        logger.info("skills_image_mode", agents_dir=str(path))
        return path

    def _isolate_skills(self, agents_path: Path) -> dict[str, Path]:
        """Copy each node's skills from ``agents/<node-id>/skills/``.

        Only nodes that declare skills in the agent definition are considered;
        their skill directories are isolated into per-skill temp directories.
        """
        tmp_dirs: dict[str, Path] = {}
        for node in self._agent_definition.get("nodes", []):
            node_id = node.get("id", "")
            node_skills = node.get("config", {}).get("skills", [])
            if not node_skills:
                continue
            agent_skills_dir = agents_path / node_id / "skills"
            if not agent_skills_dir.is_dir():
                logger.warning(
                    "agent_skills_dir_missing", node_id=node_id, path=str(agent_skills_dir)
                )
                continue
            for skill_dir in agent_skills_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                skill_name = skill_dir.name
                tmp = Path(tempfile.mkdtemp(prefix=f"skill-{skill_name}-"))
                dest = tmp / skill_name
                shutil.copytree(str(skill_dir), str(dest))
                tmp_dirs[skill_name] = tmp
                logger.info("skill_isolated", skill=skill_name, node=node_id)
        return tmp_dirs

    def _build_filesystem_routes(self) -> dict[str, Any]:
        """Build FilesystemBackend routes for each skill."""
        routes: dict[str, Any] = {}
        runtime_base = _skills_runtime_base()
        try:
            from deepagents.backends.filesystem import FilesystemBackend

            for skill_name, tmp_dir in self._tmp_dirs.items():
                route = f"{runtime_base}/skills/{skill_name}/"
                routes[route] = FilesystemBackend(
                    root_dir=str(tmp_dir / skill_name), virtual_mode=True
                )
                logger.info("skill_route_created", skill=skill_name, route=route, dest=str(tmp_dir))
        except ImportError:
            logger.warning("skill_routes_failed_deepagents_not_available")
        return routes

    def _create_skill_symlinks(self) -> None:
        """Create stable symlinks at ``<runtime_base>/skills/<name>/``."""
        stable_root = _skills_runtime_base() / "skills"
        stable_root.mkdir(parents=True, exist_ok=True)
        for skill_name, tmp_dir in self._tmp_dirs.items():
            link_path = stable_root / skill_name
            target = tmp_dir / skill_name
            if not link_path.exists():
                os.symlink(str(target), str(link_path))
                logger.info("skill_symlink_created", link=str(link_path), target=str(target))

    def _create_scratch(self, routes: dict[str, Any]) -> None:
        """Create a scratch workspace for CLI compilation output."""
        scratch_dir = Path(tempfile.mkdtemp(prefix="scratch-"))
        self._scratch_dir = str(scratch_dir)
        runtime_base = _skills_runtime_base()
        scratch_route = f"{runtime_base}/scratch/"
        try:
            from deepagents.backends.filesystem import FilesystemBackend

            routes[scratch_route] = FilesystemBackend(root_dir=str(scratch_dir), virtual_mode=True)
            logger.info("scratch_route_created", route=scratch_route, dest=str(scratch_dir))
        except ImportError:
            logger.warning("scratch_route_failed_deepagents_not_available")

        stable_scratch = runtime_base / "scratch"
        stable_scratch.parent.mkdir(parents=True, exist_ok=True)
        if not stable_scratch.exists():
            os.symlink(str(scratch_dir), str(stable_scratch))
            logger.info(
                "scratch_symlink_created", link=str(stable_scratch), target=str(scratch_dir)
            )

    def _build_composite_backend(self, routes: dict[str, Any]) -> Optional[Any]:
        """Wrap an ArtifactBackend + per-skill FilesystemBackends into a CompositeBackend."""
        if not routes:
            return None
        try:
            from deepagents.backends.composite import CompositeBackend

            return CompositeBackend(default=self._artifact_backend, routes=routes)
        except ImportError:
            logger.warning("composite_backend_failed_deepagents_not_available")
            return None

    def cleanup(self) -> None:
        """Tear down all allocated resources: temp dirs, symlinks, scratch."""
        # Remove skill symlinks
        stable_skills = _skills_runtime_base() / "skills"
        if stable_skills.exists():
            for child in stable_skills.iterdir():
                if child.is_symlink():
                    child.unlink()
            logger.info("skill_symlinks_removed")

        # Remove scratch symlink and temp dir
        stable_scratch = _skills_runtime_base() / "scratch"
        if stable_scratch.exists() and stable_scratch.is_symlink():
            stable_scratch.unlink()
            logger.info("scratch_symlink_removed")
        if self._scratch_dir and Path(self._scratch_dir).exists():
            shutil.rmtree(self._scratch_dir, ignore_errors=True)
            logger.info("scratch_temp_dir_cleaned", path=self._scratch_dir)

        # Remove skill temp dirs
        for skill_name, tmp_dir in self._tmp_dirs.items():
            target = tmp_dir / skill_name
            if target.exists():
                shutil.rmtree(str(target), ignore_errors=True)
            if tmp_dir.exists() and not any(tmp_dir.iterdir()):
                tmp_dir.rmdir()
            logger.info("skill_temp_dir_cleaned", skill=skill_name)

        # Cleanup skill router wrappers
        if self._router is not None:
            self._router.cleanup()
