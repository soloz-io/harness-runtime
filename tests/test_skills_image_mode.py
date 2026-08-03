"""Unit tests for image-baked skills support.

Covers:
  - SkillsManager sourcing skills from ``HARNESS_SKILLS_IMAGE_DIR`` without
    performing a git clone.
  - FilesystemBackend routes + symlinks built under the configured runtime base.
  - AgentSkillRouter wrapper routes using normalized skill names.
  - ``normalize_agent_definition`` rewriting image-path prefixes to the
    runtime skills base.
  - ``load_skill`` resolving content through the stable symlink path.

Does NOT require PostgreSQL, Redis, an HTTP server, or git network access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.session.skills import (
    DEFAULT_SKILLS_RUNTIME_BASE,
    ENV_SKILLS_IMAGE_DIR,
    ENV_SKILLS_RUNTIME_BASE,
    SkillsManager,
)
from core.session.skill_paths import (
    RUNTIME_SKILLS_BASE,
    normalize_agent_definition,
    normalize_skill_path,
)


class _FakeArtifactBackend:
    """Minimal stand-in for ArtifactBackend (CompositeBackend default)."""


@pytest.fixture()
def image_skills_dir(tmp_path: Path) -> Path:
    """A fake image-baked skills directory."""
    skills_dir = tmp_path / "image-skills"
    for name in ("chronixel-video", "remotion-captions"):
        (skills_dir / name).mkdir(parents=True)
        (skills_dir / name / "SKILL.md").write_text(f"# {name}\n\nGuidance for {name}.\n")
        (skills_dir / name / "references").mkdir()
        (skills_dir / name / "references" / "guide.md").write_text(f"Ref for {name}.\n")
    return skills_dir


def _definition(skills: list[str]) -> dict:
    return {
        "name": "test-agents",
        "topology": "agent-dag",
        "nodes": [
            {
                "id": "orchestrator",
                "type": "Orchestrator",
                "config": {"name": "orchestrator", "skills": []},
            },
            {
                "id": "specialist",
                "type": "Specialist",
                "config": {"name": "motion-graphics-agent", "skills": skills},
            },
        ],
    }


@pytest.fixture()
def runtime_base(tmp_path: Path) -> Path:
    base = tmp_path / "workspace"
    return base


@pytest.fixture()
def env_setup(monkeypatch: pytest.MonkeyPatch, image_skills_dir: Path, runtime_base: Path) -> None:
    monkeypatch.setenv(ENV_SKILLS_IMAGE_DIR, str(image_skills_dir))
    monkeypatch.setenv(ENV_SKILLS_RUNTIME_BASE, str(runtime_base))


def test_normalize_skill_path_maps_image_prefix_to_runtime_base() -> None:
    assert normalize_skill_path("/workspace/.oranger/skills/chronixel-video/") == (
        f"{RUNTIME_SKILLS_BASE}/chronixel-video/"
    )
    assert normalize_skill_path("/workspace/.builder/skills/remotion-captions/") == (
        f"{RUNTIME_SKILLS_BASE}/remotion-captions/"
    )
    assert normalize_skill_path("chronixel-video") == f"{RUNTIME_SKILLS_BASE}/chronixel-video/"


def test_normalize_agent_definition_rewrites_node_skills() -> None:
    original = _definition(["/workspace/.oranger/skills/chronixel-video/", "remotion-captions/"])
    normalized = normalize_agent_definition(original)

    assert normalized is not original
    assert normalized["nodes"][1]["config"]["skills"] == [
        f"{RUNTIME_SKILLS_BASE}/chronixel-video/",
        f"{RUNTIME_SKILLS_BASE}/remotion-captions/",
    ]
    # Original dict untouched
    assert original["nodes"][1]["config"]["skills"] == [
        "/workspace/.oranger/skills/chronixel-video/",
        "remotion-captions/",
    ]


def test_skills_manager_uses_image_dir_without_git_clone(
    env_setup: None, runtime_base: Path
) -> None:
    manager = SkillsManager(
        _definition(["/workspace/.oranger/skills/chronixel-video/"]),
        _FakeArtifactBackend(),
    )
    ctx = manager.initialize()

    try:
        # No git clone performed
        assert manager._git_backend is None

        # Routes are keyed by the runtime skills base
        assert ctx.composite_backend is not None
        routes = ctx.composite_backend.routes
        assert f"{runtime_base}/skills/chronixel-video/" in routes
        assert f"{runtime_base}/scratch/" in routes
        # Per-agent wrapper route exists (AgentSkillRouter uses the canonical path)
        assert "/workspace/.builder/agent/motion-graphics-agent/" in routes

        # Stable symlinks created at runtime base
        link = runtime_base / "skills" / "chronixel-video"
        assert link.is_symlink()
        assert (link / "SKILL.md").exists()
    finally:
        manager.cleanup()


def test_skills_manager_image_mode_load_skill_via_symlink(
    env_setup: None, runtime_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core import middleware  # noqa: F401
    import core.middleware.shell_middleware as shell_middleware

    monkeypatch.setattr(shell_middleware, "SKILLS_BASE", runtime_base / "skills")

    manager = SkillsManager(
        _definition(["/workspace/.oranger/skills/chronixel-video/"]),
        _FakeArtifactBackend(),
    )
    manager.initialize()

    try:
        content = shell_middleware.load_skill.invoke({"skill_name": "chronixel-video"})
        assert "# chronixel-video" in content
        ref = shell_middleware.load_skill.invoke(
            {"skill_name": "chronixel-video", "reference_path": "references/guide.md"}
        )
        assert "Ref for chronixel-video" in ref
        missing = shell_middleware.load_skill.invoke({"skill_name": "does-not-exist"})
        assert "Error" in missing
    finally:
        manager.cleanup()


def test_skills_manager_falls_back_when_image_dir_missing(
    monkeypatch: pytest.MonkeyPatch, runtime_base: Path
) -> None:
    # Point the env at a nonexistent dir — must not silently succeed.
    monkeypatch.setenv(ENV_SKILLS_IMAGE_DIR, str(runtime_base / "nope"))
    monkeypatch.setenv(ENV_SKILLS_RUNTIME_BASE, str(runtime_base))

    manager = SkillsManager(
        _definition(["/workspace/.oranger/skills/chronixel-video/"]),
        _FakeArtifactBackend(),
    )

    # Git fallback raises because AGENTREGISTRY_GIT_OWNER/REPO are unset here.
    with pytest.raises(Exception):
        manager.initialize()


def test_default_runtime_base_is_production_path() -> None:
    assert DEFAULT_SKILLS_RUNTIME_BASE == "/workspace/.builder"


def _session_definition(skills: list[str]) -> dict:
    """Definition with a model config so Session.extract_agent_config passes."""
    return {
        "name": "test-agents",
        "topology": "agent-dag",
        "nodes": [
            {
                "id": "orchestrator",
                "type": "Orchestrator",
                "config": {
                    "name": "orchestrator",
                    "model": {"model_name": "test-model"},
                    "system_prompt": "You orchestrate.",
                    "skills": [],
                },
            },
            {
                "id": "specialist",
                "type": "Specialist",
                "config": {
                    "name": "motion-graphics-agent",
                    "model": {"model_name": "test-model"},
                    "system_prompt": "You make videos.",
                    "skills": skills,
                },
            },
        ],
    }


class _FakeExecutionManager:
    checkpointer = None
    _pool = None


class _FakePublisher:
    pass


def test_session_passes_normalized_definition_to_skills_manager(env_setup: None) -> None:
    """Session must hand SkillsManager the normalized definition (single copy)."""
    from core.session.session import Session

    session = Session(
        agent_definition=_session_definition(["/workspace/.oranger/skills/chronixel-video/"]),
        input_payload={},
        execution_manager=_FakeExecutionManager(),
        publisher=_FakePublisher(),
        session_id="sess-test",
        workspace_id="ws-test",
    )
    try:
        collected = session._skills_mgr._collect_skills()
        assert collected == [f"{RUNTIME_SKILLS_BASE}/chronixel-video/"]
        # The definition SkillsManager holds is the exact normalized copy Session uses.
        assert session._skills_mgr._agent_definition is session.agent_definition
    finally:
        session.cleanup()
