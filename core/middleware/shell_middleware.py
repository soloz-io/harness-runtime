"""
Shell Middleware — provides shell execution capability to agents.

Agents use ``execute_shell`` to run CLI commands (git, node, npm, python, etc.)
in the sandboxed pod.  Builtin filesystem commands (cat, ls, grep, cp, rm, ...)
are blocked with a warning — agents must use the dedicated deepagents filesystem
tools (read_file, write_file, ls, glob, grep) which respect virtual paths and
permission rules.
"""

import subprocess

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool

logger = structlog.get_logger(__name__)

_FS_COMMANDS = {
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "xxd",
    "od",
    "hexdump",
    "bat",
    "ls",
    "find",
    "locate",
    "tree",
    "dir",
    "exa",
    "eza",
    "grep",
    "rg",
    "ripgrep",
    "ag",
    "ack",
    "sed",
    "awk",
    "cp",
    "mv",
    "rm",
    "chmod",
    "chown",
    "ln",
    "touch",
    "mkdir",
    "rmdir",
    "stat",
    "file",
    "wc",
    "du",
    "df",
    "realpath",
    "readlink",
    "tee",
}

_FS_TOOL_MAP = {
    "cat": "read_file",
    "head": "read_file",
    "tail": "read_file",
    "less": "read_file",
    "more": "read_file",
    "xxd": "read_file",
    "od": "read_file",
    "hexdump": "read_file",
    "bat": "read_file",
    "ls": "ls",
    "find": "ls",
    "locate": "ls",
    "tree": "ls",
    "dir": "ls",
    "exa": "ls",
    "eza": "ls",
    "grep": "grep",
    "rg": "grep",
    "ripgrep": "grep",
    "ag": "grep",
    "ack": "grep",
    "cp": "write_file",
    "mv": "write_file",
    "rm": "write_file",
    "chmod": "write_file",
    "chown": "write_file",
    "ln": "write_file",
    "touch": "write_file",
    "mkdir": "write_file",
    "rmdir": "write_file",
    "tee": "write_file",
    "sed": "write_file",
    "awk": "grep",
    "stat": "ls",
    "file": "read_file",
    "wc": "read_file",
    "du": "ls",
    "df": "ls",
    "realpath": "read_file",
    "readlink": "read_file",
}


def _first_token(command: str) -> str:
    """Extract the first whitespace-delimited token, stripping leading chars."""
    return command.strip().split()[0] if command.strip() else ""


@tool
def execute_shell(command: str, workdir: str | None = None, timeout: int = 300) -> dict:
    """Execute a shell command in the sandboxed pod.

    Use for git operations, running node/python scripts, package management,
    and other CLI tools.  For reading/writing/searching files, use the
    dedicated filesystem tools (read_file, write_file, ls, glob, grep) instead.

    Args:
        command: Shell command to run.
        workdir: Working directory for the command. Defaults to the process cwd.
        timeout: Timeout in seconds. Defaults to 300.

    Returns:
        On success: {"success": true, "output": str, "exit_code": int, "truncated": false}
        On failure: {"success": false, "output": str, "exit_code": int, "truncated": false}
        On timeout: {"success": false, "output": "Command timed out after <N> seconds.", "exit_code": -1, "truncated": true}
    """
    cmd = _first_token(command)
    if cmd in _FS_COMMANDS:
        tool_name = _FS_TOOL_MAP.get(cmd, "filesystem")
        return {
            "success": False,
            "output": (
                f"Warning: `{cmd}` is a filesystem operation. "
                f"Use the built-in deepagents tool (`{tool_name}`) instead, "
                f"which correctly handles virtual paths and permission rules."
            ),
            "exit_code": -1,
            "truncated": False,
        }

    logger.info("execute_shell", command=command, workdir=workdir, timeout=timeout)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir,
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
    """Provides shell execution capability to agents.

    Wire this middleware for every agent that needs CLI access — subagents
    (Workflow Architect, DAG Architect) use it to run validation commands.
    """

    tools = [execute_shell]
