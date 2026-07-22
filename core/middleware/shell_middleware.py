"""
Compile Schema Middleware — provides DSL compilation capability to agents.

Agents use ``compile_schema`` to run the wpt-engine CLI against their DSL
documents.  The tool hardcodes the command to ``node /workspace/.builder/bin/cli.cjs``
so agents only supply the file path to the ``.md`` file.
"""

import subprocess

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool

logger = structlog.get_logger(__name__)


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


class ShellMiddleware(AgentMiddleware):
    """Provides shell command execution capability to agents.

    Wire this middleware for agents that need to run CLI commands.
    Currently exposes ``compile_schema`` for DSL compilation.
    """

    tools = [compile_schema]
