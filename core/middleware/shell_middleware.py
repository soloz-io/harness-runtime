"""
Compile Schema Middleware — provides DSL compilation and action manifest
capabilities to agents.

Agents use ``compile_schema`` to run the wpt-engine CLI against their DSL
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

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool

logger = structlog.get_logger(__name__)

MANIFEST_DEFAULT_PATH = "/workspace/.builder/bin/action-manifest.json"


@tool
def compile_schema(file_path: str, timeout: int = 300) -> dict:
    """Compile and validate a DSL document against the workflow or DAG schema.

    Runs the wpt-engine CLI against the given Markdown file.  The CLI
    auto-detects the document type (workflow vs DAG) from the YAML
    frontmatter ``type`` field.

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


class ShellMiddleware(AgentMiddleware):
    """Provides shell command execution capability to agents.

    Wire this middleware for agents that need to run CLI commands or
    discover registered action types.
    Currently exposes ``compile_schema`` for DSL compilation and
    ``get_action_manifest`` for action type discovery.
    """

    tools = [compile_schema, get_action_manifest]
