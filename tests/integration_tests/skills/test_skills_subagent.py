"""Business integration test — subagent with skills sees skill directory,
not the container root filesystem.

Uses the real definition.json fixture from tests/mock/ with prompts
resolved inline. Exercises the full production stack:
  HTTP server (subprocess) → Session._init_skills() → GitBackend
  → CompositeBackend → FilesystemBackend(virtual_mode=True)
  → create_deep_agent → SubAgent spec → SkillsMiddleware

Business journey:
  User asks the orchestrator to have the workflow-architect check what
  folders exist under /workspace/.builder/skills/.  The orchestrator
  delegates, and the subagent reports back.  If the virtual_mode fix
  is working, the subagent sees only its configured skill directory
  — NOT container root (bin/, etc/).

Requires:
  AI_GATEWAY_API_KEY       — LLM gateway key (same value as DEEPSEEK_API_KEY)
  DATABASE_URL             — PostgreSQL connection string
  AGENTREGISTRY_GIT_OWNER  — GitHub owner for skills repo
  AGENTREGISTRY_GIT_REPO   — GitHub repo name for skills clone
  AGENTREGISTRY_GITHUB_TOKEN — GitHub token for skills clone auth
  redis-server             — available on PATH

Business journey assertions:
  K1: Run completes successfully (result subtype is success)
  K2: Subagent output references the skills path (confirms skills
      infrastructure was wired — the subagent was directed to that path)
  K3: Assistant output does NOT mention container root directories
      (would leak if virtual_mode=False)

Run with:
  export AI_GATEWAY_API_KEY="sk-..."
  export DATABASE_URL="postgresql://..."
  export AGENTREGISTRY_GIT_OWNER="..."
  export AGENTREGISTRY_GIT_REPO="..."
  export AGENTREGISTRY_GITHUB_TOKEN="ghp_..."
  PYTHONPATH=. uv run pytest tests/integration_tests/skills/ -v

  Or use scripts/test-setup.sh which handles all env vars + infrastructure.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from tests.integration_tests.conftest import BASE_URL, sse_server  # noqa: F401
from tests.integration_tests.helpers import assistant_text_from_frames, read_sse_frames

env_path = Path(__file__).parent.parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

FIXTURE_PATH = Path(__file__).parent.parent.parent / "mock" / "definition.json"

# Prompts keyed by placeholder name
_PROMPTS: dict[str, str] = {
    "__PROMPT_ORCHESTRATOR__": (
        "You have subagents: workflow-architect, discovery-agent, "
        "dag-architect, platform-architect. "
        "Delegate to the workflow-architect subagent to check what "
        "folders it sees under /workspace/.builder/skills/. "
        "Report back exactly what the subagent says. "
        "Do NOT use any tools yourself — only delegate to subagents."
    ),
    "__PROMPT_WORKFLOW_ARCHITECT__": (
        "List all directories under /workspace/.builder/skills/ "
        "by using the ls tool. Be thorough and report every entry "
        "you find."
    ),
    "__PROMPT_DISCOVERY_AGENT__": "",
    "__PROMPT_DAG_ARCHITECT__": "",
    "__PROMPT_PLATFORM_ARCHITECT__": "",
}


def _resolve_placeholders(obj: Any) -> Any:
    """Recursively walk the agent definition and replace placeholders."""
    if isinstance(obj, str):
        if obj == "__INJECT_MODEL_NAME__":
            return "deepseek-v4-flash"
        # Strip rubric placeholders — no rubric needed for this test
        if obj.startswith("__RUBRIC_") and obj.endswith("__"):
            return None
        # Resolve prompt placeholders
        if obj.startswith("__PROMPT_") and obj.endswith("__"):
            return _PROMPTS.get(obj, "")
        return obj
    if isinstance(obj, dict):
        return {k: _resolve_placeholders(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_placeholders(v) for v in obj]
    return obj


def _load_definition() -> dict[str, Any]:
    """Load fixture definition.json and resolve all placeholders."""
    raw = json.loads(FIXTURE_PATH.read_text())
    return _resolve_placeholders(raw)


AGENT = _load_definition()

_INPUT_PAYLOAD: dict[str, Any] = {
    "messages": [
        {
            "role": "user",
            "content": "check with workflow architect for the folders it sees",
        }
    ],
}

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_subagent_sees_skill_dir_not_container_root(sse_server: None) -> None:
    """K1+K2+K3: Subagent with skills sees its skill directory, not container root.

    Business outcome: workflow-architect accesses its configured skill
    content through the skills infrastructure without container root leaking.
    """
    session_id = str(uuid.uuid4())

    httpx.post(
        f"{BASE_URL}/session/{session_id}/message",
        json={
            "message": "check with workflow architect for the folders it sees",
            "agent_definition": AGENT,
            "input_payload": dict(_INPUT_PAYLOAD),
            "workspace_id": "test-workspace",
        },
        timeout=30.0,
    )

    with httpx.stream(
        "GET",
        f"{BASE_URL}/event?session_id={session_id}",
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(120.0),
    ) as sse_resp:
        frames = read_sse_frames(sse_resp)

    result = frames[-1]
    assert result["type"] == "result"
    assert result["subtype"] == "success", f"K1 fail: expected success, got {result['subtype']}"

    assistant_text = assistant_text_from_frames(frames)

    # K2: Subagent output references the skills path (confirms the skills
    # infrastructure wired the directory — even if it happens to be empty).
    assert "skills" in assistant_text.lower(), (
        f"K2 fail: subagent output should reference the skills path. "
        f"Content preview: {assistant_text[:500]}"
    )

    # K3: virtual_mode=True prevents container root leak.
    assert "/bin/" not in assistant_text, (
        "K3 fail: subagent should NOT see container /bin/. "
        "If this fails, the virtual_mode fix may be missing or broken."
    )
    assert "/etc/" not in assistant_text, (
        "K3 fail: subagent should NOT see container /etc/. "
        "If this fails, the virtual_mode fix may be missing or broken."
    )
