"""Session lifecycle — manages agent session state and turn execution.

Creates the agent graph on first invocation, persists messages across
turns, and delegates to executor.py for run/stream logic.
"""

import uuid
from pathlib import Path
from typing import Any, Optional

import structlog

from core.embedded_tool_loader import ToolLoadingError, load_tool_implementations
from core.event_publisher import EventPublisher
from core.executor import ExecutionManager
from core.factory import build_agent_from_definition
from core.tool_registry import ToolRegistry

logger = structlog.get_logger(__name__)


class Session:
    def __init__(
        self,
        agent_definition: dict[str, Any],
        input_payload: dict[str, Any],
        execution_manager: ExecutionManager,
        publisher: EventPublisher,
        session_id: Optional[str] = None,
        workspace_id: str = "",
    ) -> None:
        self.session_id = session_id or f"sess_{uuid.uuid4().hex[:24]}"
        self.workspace_id = workspace_id
        self.agent_definition = agent_definition
        self.base_payload = input_payload
        self.execution_manager = execution_manager
        self.publisher = publisher
        self.turns = 0
        if not workspace_id:
            raise ValueError("workspace_id is required")
        self._tool_registry: ToolRegistry | None = None
        self._initialized = False
        self._backend: Any = None
        self._composite_backend: Any = None
        self._skill_git_backend: Any = None
        model_name: str | None = None
        nodes = agent_definition.get("nodes", [])
        if nodes:
            node_config = nodes[0].get("config", {})
            model_cfg = node_config.get("model", {})
            model_name = model_cfg.get("model_name") or model_cfg.get("model")
        if not model_name:
            raise ValueError(
                "No model name found in agent definition. "
                "Set config.model.model_name in the first node, "
                "or set LLM_MODEL_NAME env var"
            )
        self.model_name: str = model_name
        self.checkpointer = execution_manager.checkpointer

        # Build ArtifactBackend once — reused for tool loading + graph wiring
        self._backend = None
        if hasattr(self.execution_manager, "_pool") and self.execution_manager._pool is not None:
            from core.backends.artifact import ArtifactBackend

            self._backend = ArtifactBackend(
                workspace_id=self.workspace_id,
                session_id=self.session_id,
                pool=self.execution_manager._pool,
            )

        # Initialize skills backend (CompositeBackend wrapping ArtifactBackend + per-skill FilesystemBackends)
        self._composite_backend = None
        self._skill_git_backend = None
        self._scratch_dir: str | None = None
        self._skill_router: Any = None
        self._init_skills()

    def _init_skills(self) -> None:
        """Clone skills repo, isolate each skill to its own temp dir, build per-skill routes, create stable CLI symlink, delete clone."""
        import shutil
        import tempfile
        from pathlib import Path

        skills_paths: list[str] = []
        for node in self.agent_definition.get("nodes", []):
            node_skills = node.get("config", {}).get("skills", [])
            skills_paths.extend(node_skills)

        if not skills_paths:
            logger.info("no_skills_defined_in_agent_definition")
            return

        from core.integration.git_backend import GitBackend

        gb = GitBackend("packages/master-chief-agent/src/skills")
        self._skill_git_backend = gb

        cli_src = gb.repo_path / "packages" / "wpt-engine" / "bin" / "cli.cjs"

        skill_routes: dict[str, Any] = {}
        skill_tmp_dirs: dict[str, Path] = {}

        for skill_dir in gb.path.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_name = skill_dir.name
            tmp = Path(tempfile.mkdtemp(prefix=f"skill-{skill_name}-"))
            dest = tmp / skill_name
            shutil.copytree(str(skill_dir), str(dest))
            skill_tmp_dirs[skill_name] = tmp

            if cli_src.exists():
                engine_dir = dest / "engine"
                engine_dir.mkdir(parents=True, exist_ok=True)
                cli_target = engine_dir / "cli.cjs"
                if not cli_target.exists():
                    shutil.copy2(str(cli_src), str(cli_target))

            try:
                from deepagents.backends.filesystem import FilesystemBackend

                route = f"/workspace/.builder/skills/{skill_name}/"
                skill_routes[route] = FilesystemBackend(root_dir=str(dest), virtual_mode=True)
                logger.info("skill_route_created", skill=skill_name, route=route, dest=str(dest))
            except ImportError:
                logger.warning("skill_route_failed_deepagents_not_available", skill=skill_name)

        # Create stable symlinks at /workspace/.builder/skills/<name>/ for execute_shell access
        self._create_skill_symlinks(skill_tmp_dirs)

        # Create scratch workspace for CLI compilation files (not a skill — scratch disk for execute_shell)
        import os as _os

        scratch_dir = Path(tempfile.mkdtemp(prefix="scratch-"))
        self._scratch_dir = str(scratch_dir)
        scratch_route = "/workspace/.builder/scratch/"
        try:
            from deepagents.backends.filesystem import FilesystemBackend

            skill_routes[scratch_route] = FilesystemBackend(
                root_dir=str(scratch_dir), virtual_mode=True
            )
            logger.info("scratch_route_created", route=scratch_route, dest=str(scratch_dir))
        except ImportError:
            logger.warning("scratch_route_failed_deepagents_not_available")

        # Create stable symlink at /workspace/.builder/scratch/ for execute_shell access
        stable_scratch = Path("/workspace/.builder/scratch")
        stable_scratch.parent.mkdir(parents=True, exist_ok=True)
        if not stable_scratch.exists():
            _os.symlink(str(scratch_dir), str(stable_scratch))
            logger.info(
                "scratch_symlink_created", link=str(stable_scratch), target=str(scratch_dir)
            )

        # Copy CLI to a stable path for execute_shell
        self._copy_cli_to_stable_path(cli_src)

        # Build per-agent skill wrapper routes for SkillsMiddleware discovery
        from core.skill_router import AgentSkillRouter

        self._skill_router = AgentSkillRouter(self.agent_definition, skill_tmp_dirs)
        skill_routes.update(self._skill_router.build_routes())

        # Delete the full clone to prevent execute_shell from finding it
        shutil.rmtree(str(gb.repo_path), ignore_errors=True)
        logger.info("skills_clone_deleted", path=str(gb.repo_path))

        # Build CompositeBackend with per-skill FilesystemBackend routes.
        # Default=ArtifactBackend: non-routed paths go to DB.
        # Route per-skill: FilesystemBackend serves skill files from disk.
        # The stable symlinks (created above) let execute_shell resolve skill paths
        # on the actual filesystem.  SKILL.md instructs agents to write DSL files
        # into the skill route (disk-backed) for CLI validation.
        if skill_routes:
            try:
                from deepagents.backends.composite import CompositeBackend

                self._composite_backend = CompositeBackend(
                    default=self._backend,
                    routes=skill_routes,
                )
                logger.info(
                    "composite_backend_built_with_per_skill_routes",
                    route_count=len(skill_routes),
                )
            except ImportError:
                logger.warning("composite_backend_failed_deepagents_not_available")

    def _create_skill_symlinks(self, skill_tmp_dirs: dict[str, Path]) -> None:
        """Create stable symlinks at /workspace/.builder/skills/<name>/ → temp dirs.

        These symlinks let execute_shell (which runs on the actual filesystem)
        resolve skill files via stable paths instead of random temp dir names.
        """
        import os

        stable_root = Path("/workspace/.builder/skills")
        stable_root.mkdir(parents=True, exist_ok=True)
        for skill_name, tmp_dir in skill_tmp_dirs.items():
            link_path = stable_root / skill_name
            target = tmp_dir / skill_name
            if not link_path.exists():
                os.symlink(str(target), str(link_path))
                logger.info("skill_symlink_created", link=str(link_path), target=str(target))

    def _copy_cli_to_stable_path(self, cli_src: Path) -> None:
        """Copy the wpt-engine CLI to a stable path for execute_shell access.

        The CLI is available at /workspace/.builder/bin/cli.cjs so agents
        can run: execute_shell(command="node /workspace/.builder/bin/cli.cjs /tmp/...")
        """
        import shutil

        if not cli_src.exists():
            logger.warning("cli_source_not_found", path=str(cli_src))
            return
        stable_cli_dir = Path("/workspace/.builder/bin")
        stable_cli_dir.mkdir(parents=True, exist_ok=True)
        stable_cli = stable_cli_dir / "cli.cjs"
        if not stable_cli.exists():
            shutil.copy2(str(cli_src), str(stable_cli))
            logger.info("cli_copied_to_stable_path", path=str(stable_cli))

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return

        self._tool_registry = ToolRegistry()

        tool_definitions = self.agent_definition.get("tool_definitions", [])
        if tool_definitions:
            try:
                load_tool_implementations(tool_definitions, self._tool_registry)
            except ToolLoadingError as e:
                logger.error("tool_loading_failed", error=str(e))
                raise

        self._initialized = True

    def initialize(self, resume_payload: Optional[Any] = None) -> None:
        if resume_payload:
            logger.info("session_resuming", session_id=self.session_id, has_resume=True)
        else:
            logger.info("session_initialized", session_id=self.session_id)
        self.resume_payload = resume_payload

    async def async_run_turn(
        self, user_content: str = "", publisher: Optional[EventPublisher] = None
    ) -> str:
        await self._ensure_initialized()

        self.turns += 1

        input_payload = dict(self.base_payload)
        messages = input_payload.get("messages", [])
        if user_content:
            messages = messages + [{"role": "user", "content": user_content}]
        input_payload["messages"] = messages

        compiled_graph = build_agent_from_definition(
            self.agent_definition,
            checkpointer=self.checkpointer,
            tool_registry=self._tool_registry,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            backend=self._backend,
            composite_backend=self._composite_backend,
        )

        resume = getattr(self, "resume_payload", None)
        if resume is not None:
            self.resume_payload = None  # consume once

        result = await self.execution_manager.async_execute(
            graph=compiled_graph,
            session_id=self.session_id,
            input_payload=input_payload,
            model_name=self.model_name,
            publisher=publisher or self.publisher,
            agent_definition=self.agent_definition,
            num_turns=self.turns,
            resume_payload=resume,
        )

        return result

    def run_turn(self, user_content: str = "") -> str:
        self.turns += 1

        input_payload = dict(self.base_payload)
        messages = input_payload.get("messages", [])
        if user_content:
            messages = messages + [{"role": "user", "content": user_content}]
        input_payload["messages"] = messages

        compiled_graph = build_agent_from_definition(
            self.agent_definition,
            checkpointer=self.checkpointer,
            tool_registry=self._tool_registry,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            backend=self._backend,
            composite_backend=self._composite_backend,
        )

        resume = getattr(self, "resume_payload", None)
        if resume is not None:
            self.resume_payload = None  # consume once

        result = self.execution_manager.execute(
            graph=compiled_graph,
            session_id=self.session_id,
            input_payload=input_payload,
            model_name=self.model_name,
            agent_definition=self.agent_definition,
            num_turns=self.turns,
            resume_payload=resume,
        )

        return result

    def resume_turn(self, resume_payload: Any) -> str:
        self.turns += 1
        compiled_graph = build_agent_from_definition(
            self.agent_definition,
            checkpointer=self.checkpointer,
            tool_registry=self._tool_registry,
            workspace_id=self.workspace_id,
            session_id=self.session_id,
            backend=self._backend,
            composite_backend=self._composite_backend,
        )
        result = self.execution_manager.execute(
            graph=compiled_graph,
            session_id=self.session_id,
            input_payload={},
            model_name=self.model_name,
            agent_definition=self.agent_definition,
            num_turns=self.turns,
            resume_payload=resume_payload,
        )
        return result

    async def cleanup(self) -> None:
        import shutil
        from pathlib import Path

        if self._skill_git_backend is not None:
            self._skill_git_backend.cleanup()
        if self._composite_backend is not None:
            routes = getattr(self._composite_backend, "routes", {})
            for route_prefix, backend in routes.items():
                root_dir = getattr(backend, "_root_dir", None)
                if root_dir and Path(root_dir).exists():
                    shutil.rmtree(str(root_dir), ignore_errors=True)
                    logger.info("skill_temp_dir_cleaned", route=route_prefix, path=root_dir)
                    # Also clean up the parent mkdtemp dir if empty
                    parent = Path(root_dir).parent
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                        logger.info("skill_parent_temp_dir_cleaned", path=str(parent))
        # Remove stable symlinks
        stable_skills = Path("/workspace/.builder/skills")
        if stable_skills.exists():
            for child in stable_skills.iterdir():
                if child.is_symlink():
                    child.unlink()
            logger.info("skill_symlinks_removed")
        # Remove scratch symlink and temp dir
        stable_scratch = Path("/workspace/.builder/scratch")
        if stable_scratch.exists():
            if stable_scratch.is_symlink():
                stable_scratch.unlink()
            logger.info("scratch_symlink_removed")
        if self._scratch_dir and Path(self._scratch_dir).exists():
            shutil.rmtree(self._scratch_dir, ignore_errors=True)
            logger.info("scratch_temp_dir_cleaned", path=self._scratch_dir)

        # Clean up agent skill wrappers
        if self._skill_router is not None:
            self._skill_router.cleanup()

        # Remove stable CLI copy
        stable_cli = Path("/workspace/.builder/bin/cli.cjs")
        if stable_cli.exists():
            stable_cli.unlink()
            logger.info("stable_cli_removed", path=str(stable_cli))
