"""
Skill Loading Middleware — provides the ``load_skill`` capability to agents.

Agents call ``load_skill`` to retrieve the full text of their assigned domain
SKILL (and optionally a reference file within it) before generating artifacts.

DSL compilation (``compile_schema``) and action-manifest discovery
(``get_action_manifest``) were migrated out of this middleware into per-agent
CLI tools exposed via ``run_tool`` (see ``CustomToolMiddleware`` and the
``agents/shared/tools/`` directory in the builder image).
"""

from pathlib import Path
from typing import Optional

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool

logger = structlog.get_logger(__name__)

SKILLS_BASE = Path("/workspace/.builder/skills")


@tool("load_skill")
def load_skill(skill_name: str, reference_path: Optional[str] = None) -> str:
    """Load a domain SKILL (and optionally a reference file within it) and
    return its full text content.

    Call this before generating any domain artifacts.  The SKILL defines
    your schema, boundaries, ownership, generation rules, and validation
    criteria.

    Every specialist has exactly one SKILL assigned to it.  Pass the
    exact name from your ``<agent_skills>`` declaration.

    When the SKILL tells you to consult a supporting file (e.g.
    ``references/derivation-algorithm.md``), pass its path as
    ``reference_path``.

    Args:
        skill_name: The name of the skill to load.
        reference_path: Optional path to a supporting file within the
            skill directory (e.g. ``references/derivation-algorithm.md``).

    Returns:
        The full text content of the requested file, or an error message
        if the file is not found or cannot be read.
    """
    if reference_path is None:
        file_path = SKILLS_BASE / skill_name / "SKILL.md"
    else:
        _validate_path(skill_name, reference_path)
        file_path = SKILLS_BASE / skill_name / reference_path

    if not file_path.exists():
        logger.warning("load_skill_not_found", skill_name=skill_name, path=str(file_path))
        available = _available_skills()
        return (
            f"Error: Skill file not found at {file_path}.\n"
            f"Available skills: {available}\n"
            f"Make sure you are using the correct skill name "
            f"(see your ``<agent_skills>`` declaration)."
        )

    try:
        content = file_path.read_text("utf-8")
        logger.info(
            "load_skill_loaded", skill_name=skill_name, path=str(file_path), size=len(content)
        )
        return content
    except Exception as e:
        logger.error(
            "load_skill_read_error", skill_name=skill_name, path=str(file_path), error=str(e)
        )
        return f"Error: Failed to read skill file '{file_path}': {e}"


def _validate_path(skill_name: str, reference_path: str) -> None:
    """Guard against path traversal outside the skill directory.

    Skills may be symlinked (e.g. from a temp dir), so compare against the
    resolved skill directory rather than the symlink path.
    """
    resolved = (SKILLS_BASE / skill_name / reference_path).resolve()
    skill_dir = (SKILLS_BASE / skill_name).resolve()
    if not str(resolved).startswith(str(skill_dir)):
        raise ValueError(
            f"Path traversal detected: '{reference_path}' resolves to "
            f"{resolved}, which is outside skill directory {skill_dir}"
        )


def _available_skills() -> str:
    if not SKILLS_BASE.exists():
        return "(none — skills directory does not exist)"
    names = sorted(
        d.name for d in SKILLS_BASE.iterdir() if d.is_dir() and (d / "SKILL.md").exists()
    )
    return ", ".join(names) if names else "(none)"


class ShellMiddleware(AgentMiddleware):
    """Provides skill loading capability to agents.

    Wire this middleware for agents that need to read their assigned domain
    SKILL files.
    Currently exposes ``load_skill`` for skill retrieval.
    """

    tools = [load_skill]
