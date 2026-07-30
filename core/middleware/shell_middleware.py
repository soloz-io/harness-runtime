"""
Compile Schema Middleware — provides DSL compilation and action manifest
capabilities to agents.

Agents use ``compile_schema`` to run the workflow-engine CLI against their DSL
documents.  The tool hardcodes the command to ``node /workspace/.builder/bin/cli.cjs``
so agents only supply the file path to the ``.md`` file.

Agents use ``get_action_manifest`` to retrieve the registered action types and
their metadata (capabilities, config fields, etc.) — the same data that drives
schema compilation and known-type validation.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool

logger = structlog.get_logger(__name__)

SKILLS_BASE = Path("/workspace/.builder/skills")
MANIFEST_DEFAULT_PATH = "/workspace/.builder/bin/action-manifest.json"


@tool
def compile_schema(file_path: str, timeout: int = 300) -> dict:
    """Compile and validate a DSL document against the business, product, workflow, or DAG schema.

    Runs the workflow-engine CLI against the given Markdown file.  The CLI
    auto-detects the document type (business, product, workflow, or DAG) from
    the YAML frontmatter ``type`` field.

    Args:
        file_path: Absolute path to the ``.md`` DSL file.
        timeout: Timeout in seconds. Defaults to 300.

    Returns:
        On success: {"success": true, "output": str, "exit_code": int, "truncated": false}
        On failure: {"success": false, "output": str, "exit_code": int, "truncated": false}
        On timeout: {"success": false, "output": "Command timed out after <N> seconds.", "exit_code": -1, "truncated": true}
    """
    command = f"node /workspace/.builder/bin/cli.cjs {file_path}"

    logger.info("compile_schema", file_path=file_path, timeout=timeout)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "success": result.returncode == 0,
            "output": result.stdout if result.returncode == 0 else result.stderr,
            "exit_code": result.returncode,
            "truncated": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "output": f"Command timed out after {timeout} seconds.",
            "exit_code": -1,
            "truncated": True,
        }
    except Exception as e:
        return {
            "success": False,
            "output": f"Error executing command: {e}",
            "exit_code": -1,
            "truncated": False,
        }


@tool
def get_action_manifest() -> list[dict]:
    """Get all registered action types with their metadata.

    Returns the action manifest — a list of every registered action type
    with its label, description, category, required/optional config fields,
    capabilities, and usage guidance.  Agents can use this to discover
    available actions and their input schemas when building workflow DSL
    documents.

    Returns:
        A list of action type definition dicts, each containing:
        actionType, label, description, category, capabilities,
        configFields (name, type, required, description), and useWhen.
        Returns an empty list if the manifest file cannot be read.
    """
    manifest_path = os.environ.get("ACTION_MANIFEST_PATH", MANIFEST_DEFAULT_PATH)
    path = Path(manifest_path)

    logger.info("get_action_manifest", path=str(path))

    if not path.exists():
        logger.warning("action_manifest_not_found", path=str(path))
        return []

    try:
        raw = path.read_text("utf-8")
        manifest = json.loads(raw)
        logger.info("action_manifest_loaded", action_type_count=len(manifest))
        return manifest
    except (json.JSONDecodeError, OSError) as e:
        logger.error("action_manifest_read_error", path=str(path), error=str(e))
        return []


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
    """Provides shell command execution capability to agents.

    Wire this middleware for agents that need to run CLI commands or
    discover registered action types.
    Currently exposes ``compile_schema`` for DSL compilation and
    ``get_action_manifest`` for action type discovery.
    """

    tools = [compile_schema, get_action_manifest, load_skill]
