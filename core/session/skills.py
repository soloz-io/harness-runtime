import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SkillsContext:
    composite_backend: Optional[Any] = None
    skill_router: Optional[Any] = None


class SkillsManager:
    """Owns the full skills lifecycle: setup and teardown.

    On initialization it clones the skills repository, creates per-skill
    temporary directories, wires FilesystemBackend routes, builds a
    CompositeBackend, and creates stable filesystem symlinks for
    ``execute_shell`` access.  ``cleanup()`` tears everything down.
    """

    def __init__(
        self,
        agent_definition: dict[str, Any],
        artifact_backend: Any,
    ) -> None:
        self._agent_definition = agent_definition
        self._artifact_backend = artifact_backend
        self._git_backend: Optional[Any] = None
        self._tmp_dirs: dict[str, Path] = {}
        self._scratch_dir: Optional[str] = None
        self._router: Optional[Any] = None

    def initialize(self) -> SkillsContext:
        """Clone skills repo, set up per-skill routes and composite backend."""
        skills_paths = self._collect_skills()
        if not skills_paths:
            logger.info("no_skills_defined_in_agent_definition")
            return SkillsContext()

        self._git_backend = self._clone_repo()
        cli_src = self._git_backend.repo_path / "packages" / "wpt-engine" / "bin" / "cli.cjs"

        self._tmp_dirs = self._isolate_skills(self._git_backend.path)
        self._copy_cli_to_skill_dirs(cli_src)

        skill_routes = self._build_filesystem_routes()
        self._create_skill_symlinks()
        self._create_scratch(skill_routes)
        self._copy_cli_to_stable_path(cli_src)

        # Build per-agent skill wrapper routes
        from core.skill_router import AgentSkillRouter

        self._router = AgentSkillRouter(self._agent_definition, self._tmp_dirs)
        skill_routes.update(self._router.build_routes())

        # Delete the full clone to prevent execute_shell from finding it
        shutil.rmtree(str(self._git_backend.repo_path), ignore_errors=True)
        logger.info("skills_clone_deleted", path=str(self._git_backend.repo_path))

        composite_backend = self._build_composite_backend(skill_routes)
        return SkillsContext(composite_backend=composite_backend, skill_router=self._router)

    # ── internal helpers ──────────────────────────────────────────────

    def _collect_skills(self) -> list[str]:
        skills: list[str] = []
        for node in self._agent_definition.get("nodes", []):
            node_skills = node.get("config", {}).get("skills", [])
            skills.extend(node_skills)
        return skills

    def _clone_repo(self) -> Any:
        from core.integration.git_backend import GitBackend

        return GitBackend("packages/master-chief-agent/src/skills")

    def _isolate_skills(self, repo_path: Path) -> dict[str, Path]:
        """Copy each skill directory into an isolated temp directory."""
        tmp_dirs: dict[str, Path] = {}
        for skill_dir in repo_path.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_name = skill_dir.name
            tmp = Path(tempfile.mkdtemp(prefix=f"skill-{skill_name}-"))
            dest = tmp / skill_name
            shutil.copytree(str(skill_dir), str(dest))
            tmp_dirs[skill_name] = tmp
        return tmp_dirs

    def _copy_cli_to_skill_dirs(self, cli_src: Path) -> None:
        """Copy the wpt-engine CLI into each skill's engine directory."""
        if not cli_src.exists():
            return
        for skill_name, tmp_dir in self._tmp_dirs.items():
            engine_dir = tmp_dir / skill_name / "engine"
            engine_dir.mkdir(parents=True, exist_ok=True)
            cli_target = engine_dir / "cli.cjs"
            if not cli_target.exists():
                shutil.copy2(str(cli_src), str(cli_target))

    def _build_filesystem_routes(self) -> dict[str, Any]:
        """Build FilesystemBackend routes for each skill."""
        routes: dict[str, Any] = {}
        try:
            from deepagents.backends.filesystem import FilesystemBackend

            for skill_name, tmp_dir in self._tmp_dirs.items():
                route = f"/workspace/.builder/skills/{skill_name}/"
                routes[route] = FilesystemBackend(
                    root_dir=str(tmp_dir / skill_name), virtual_mode=True
                )
                logger.info("skill_route_created", skill=skill_name, route=route, dest=str(tmp_dir))
        except ImportError:
            logger.warning("skill_routes_failed_deepagents_not_available")
        return routes

    def _create_skill_symlinks(self) -> None:
        """Create stable symlinks at ``/workspace/.builder/skills/<name>/``."""
        stable_root = Path("/workspace/.builder/skills")
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
        scratch_route = "/workspace/.builder/scratch/"
        try:
            from deepagents.backends.filesystem import FilesystemBackend

            routes[scratch_route] = FilesystemBackend(root_dir=str(scratch_dir), virtual_mode=True)
            logger.info("scratch_route_created", route=scratch_route, dest=str(scratch_dir))
        except ImportError:
            logger.warning("scratch_route_failed_deepagents_not_available")

        stable_scratch = Path("/workspace/.builder/scratch")
        stable_scratch.parent.mkdir(parents=True, exist_ok=True)
        if not stable_scratch.exists():
            os.symlink(str(scratch_dir), str(stable_scratch))
            logger.info(
                "scratch_symlink_created", link=str(stable_scratch), target=str(scratch_dir)
            )

    def _copy_cli_to_stable_path(self, cli_src: Path) -> None:
        """Copy the wpt-engine CLI to ``/workspace/.builder/bin/cli.cjs``."""
        if not cli_src.exists():
            logger.warning("cli_source_not_found", path=str(cli_src))
            return
        stable_dir = Path("/workspace/.builder/bin")
        stable_dir.mkdir(parents=True, exist_ok=True)
        stable_cli = stable_dir / "cli.cjs"
        if not stable_cli.exists():
            shutil.copy2(str(cli_src), str(stable_cli))
            logger.info("cli_copied_to_stable_path", path=str(stable_cli))

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
        """Tear down all allocated resources: temp dirs, symlinks, clone."""
        # Cleanup git backend
        if self._git_backend is not None:
            self._git_backend.cleanup()

        # Remove skill symlinks
        stable_skills = Path("/workspace/.builder/skills")
        if stable_skills.exists():
            for child in stable_skills.iterdir():
                if child.is_symlink():
                    child.unlink()
            logger.info("skill_symlinks_removed")

        # Remove scratch symlink and temp dir
        stable_scratch = Path("/workspace/.builder/scratch")
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

        # Remove stable CLI copy
        stable_cli = Path("/workspace/.builder/bin/cli.cjs")
        if stable_cli.exists():
            stable_cli.unlink()
            logger.info("stable_cli_removed", path=str(stable_cli))
