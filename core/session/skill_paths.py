"""Skill-path normalization for agent definitions.

Agent definitions declare skills as absolute paths (e.g. the oranger
definition uses ``/workspace/.oranger/skills/chronixel-video/``). The runtime
exposes skills through FilesystemBackend routes and symlinks rooted at
``/workspace/.builder/skills/<name>/``, so LLM-visible paths must be rewritten
to match the routed backend. The skill *name* (path basename) is the join key
used by ``AgentSkillRouter`` and the ``load_skill`` tool, so only the prefix
changes.

Normalization is applied to a deep copy of the agent definition so callers
never observe mutation of their original dict.
"""

from copy import deepcopy
from typing import Any

# Runtime base path where skills are routed/symlinked.
RUNTIME_SKILLS_BASE = "/workspace/.builder/skills"


def normalize_agent_definition(agent_definition: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``agent_definition`` with skill paths normalized.

    Each ``node.config.skills`` entry is rewritten to
    ``/workspace/.builder/skills/<basename>/``. Entries that cannot be reduced
    to a bare name (e.g. empty strings) are left untouched.
    """
    normalized = deepcopy(agent_definition)

    for node in normalized.get("nodes", []):
        config = node.get("config")
        if not isinstance(config, dict):
            continue
        skills = config.get("skills")
        if not isinstance(skills, list):
            continue
        config["skills"] = [normalize_skill_path(s) for s in skills]

    return normalized


def normalize_skill_path(skill_path: str) -> str:
    """Rewrite a single skill path to the runtime skills base, keeping its name."""
    stripped = skill_path.rstrip("/")
    name = stripped.rsplit("/", 1)[-1] if stripped else ""
    if not name:
        return skill_path
    return f"{RUNTIME_SKILLS_BASE}/{name}/"
