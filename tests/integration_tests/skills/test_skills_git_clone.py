"""Standalone test — GitBackend clone produces skill files.

Verifies the git clone of the skills repo at session init actually
produces the expected skill directories and SKILL.md files.

Does NOT require an HTTP server, PostgreSQL, or Redis — only env vars:
  AGENTREGISTRY_GIT_OWNER
  AGENTREGISTRY_GIT_REPO
  AGENTREGISTRY_GITHUB_TOKEN
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

env_path = Path(__file__).parent.parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

GIT_REF = "packages/builders/src/skills"

# Skills expected by the test fixture definition.json
EXPECTED_SKILLS: set[str] = {
    "workflow-model-designer",
    "dag-model-designer",
}

REQUIRED_ENV = {
    "AGENTREGISTRY_GIT_OWNER",
    "AGENTREGISTRY_GIT_REPO",
    "AGENTREGISTRY_GITHUB_TOKEN",
}


def test_git_clone_produces_skill_files() -> None:
    """GitBackend clone + verify skill directories and SKILL.md exist."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    assert not missing, f"Missing required env vars: {missing}"

    from core.integration.git_backend import GitBackend

    gb = GitBackend(GIT_REF)

    try:
        skills_dir = gb.path
        assert skills_dir.exists(), f"Skills dir not found at {skills_dir}"
        assert skills_dir.is_dir(), f"Skills path is not a directory: {skills_dir}"

        subdirs = {p.name for p in skills_dir.iterdir() if p.is_dir()}
        missing_skills = EXPECTED_SKILLS - subdirs
        assert not missing_skills, (
            f"Expected skills missing after clone: {missing_skills}. Found subdirs: {subdirs}"
        )

        for skill_name in EXPECTED_SKILLS:
            skill_dir = skills_dir / skill_name
            skill_md = skill_dir / "SKILL.md"
            assert skill_md.exists(), (
                f"Missing SKILL.md for '{skill_name}' at {skill_md}. "
                f"Contents: {[str(p.relative_to(skill_dir)) for p in sorted(skill_dir.rglob('*'))]}"
            )
            content = skill_md.read_text()
            assert len(content) > 50, (
                f"SKILL.md for '{skill_name}' is too short ({len(content)} chars) — "
                f"may be a stub or empty template"
            )

    finally:
        gb.cleanup()
