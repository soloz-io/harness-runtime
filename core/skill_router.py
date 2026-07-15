"""Per-agent skill wrapper routes for SkillsMiddleware discovery.

Each skill-using agent gets a wrapper directory containing copies of
only its designated skills. This ensures ``SkillsMiddleware.ls()``
discovers only that agent's skills — no cross-contamination.

SRP: Only owns creation + cleanup of agent wrapper directories.
This does NOT own per-skill I/O isolation (per-skill routes in session.py)
or FilesystemPermission rules (subagent_builder.py).
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class AgentSkillRouter:
    """Build per-agent skill wrapper routes for SkillsMiddleware discovery.

    For each agent node that declares skills, creates a wrapper temp dir
    with copies of that agent's skill files.  Returns FilesystemBackend
    routes keyed by ``/workspace/.builder/agent/<agent_name>/``.

    Usage::

        router = AgentSkillRouter(agent_definition, skill_tmp_dirs)
        routes = router.build_routes()
        # merge routes into CompositeBackend
        router.cleanup()
    """

    def __init__(
        self,
        agent_definition: dict[str, Any],
        skill_tmp_dirs: dict[str, Path],
    ) -> None:
        self._nodes = agent_definition.get("nodes", [])
        self._skill_tmp_dirs = skill_tmp_dirs
        self._wrapper_dirs: dict[str, Path] = {}

    def build_routes(self) -> dict[str, Any]:
        """Return ``{"/workspace/.builder/agent/<name>/": FilesystemBackend}``.

        Each wrapper directory contains copies of the skill files assigned
        to that agent (cross-temp-dir symlinks would be silently skipped
        by ``FilesystemBackend(virtual_mode=True)``).
        """
        from deepagents.backends.filesystem import FilesystemBackend

        routes: dict[str, Any] = {}

        for node in self._nodes:
            config = node.get("config", {})
            agent_name = config.get("name", "")
            node_skills: list[str] = config.get("skills", [])
            if not agent_name or not node_skills:
                continue

            wrapper = Path(tempfile.mkdtemp(prefix=f"agent-{agent_name}-"))
            self._wrapper_dirs[agent_name] = wrapper

            for skill_path in node_skills:
                skill_name = skill_path.rstrip("/").rsplit("/", 1)[-1]
                skill_tmp = self._skill_tmp_dirs.get(skill_name)
                if skill_tmp is None:
                    logger.warning(
                        "skill_tmp_dir_not_found",
                        agent=agent_name,
                        skill=skill_name,
                    )
                    continue
                src = skill_tmp / skill_name
                dst = wrapper / skill_name
                if src.exists():
                    shutil.copytree(str(src), str(dst), symlinks=False, dirs_exist_ok=True)
                    logger.info(
                        "agent_skill_copied",
                        agent=agent_name,
                        skill=skill_name,
                        source=str(src),
                    )

            route = f"/workspace/.builder/agent/{agent_name}/"
            routes[route] = FilesystemBackend(root_dir=str(wrapper), virtual_mode=True)
            logger.info(
                "agent_skill_route_created",
                agent=agent_name,
                route=route,
                wrapper=str(wrapper),
            )

        return routes

    def cleanup(self) -> None:
        """Remove all wrapper temp dirs.

        Called by ``Session.cleanup()`` as a safety net — the wrapper
        dirs are normally cleaned via CompositeBackend route iteration.
        """
        for agent_name, wrapper in self._wrapper_dirs.items():
            if wrapper.exists():
                shutil.rmtree(str(wrapper), ignore_errors=True)
                logger.info("agent_wrapper_cleaned", agent=agent_name, path=str(wrapper))
