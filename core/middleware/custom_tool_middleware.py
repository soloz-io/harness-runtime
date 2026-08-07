"""Custom Tool Middleware — provides a generic ``run_tool`` dispatch to agents.

When an agent node has a ``tools/`` folder baked into the container image,
``ToolsManager`` discovers it and ``CustomToolMiddleware`` exposes a single
``run_tool`` tool that the LLM calls to execute any CLI script in that folder.

The tool runs ``python <tools_dir>/<tool_name>.py <args>`` via subprocess and
returns a structured result.  The agent never sees filesystem paths or manages
dependencies — only the tool name and CLI flags.
"""

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool as lc_tool

logger = structlog.get_logger(__name__)


def _build_run_tool(tools_dir: Path):
    """Factory: create a ``run_tool`` closure bound to *tools_dir*."""

    @lc_tool("run_tool")
    def run_tool(tool_name: str, cli_args: str = "", timeout: int = 600) -> dict[str, Any]:
        """Execute a pipeline CLI tool by name.

        Runs ``python <tools_dir>/<tool_name>.py <cli_args>`` and returns the
        result.  The working directory is the tools folder so relative
        imports (``workdir``, ``services``) resolve correctly.

        Args:
            tool_name: Script name without the ``.py`` extension (e.g. ``seg_cli``).
            cli_args:  CLI flags as a single string (e.g. ``--scene-duration 6.0``).
            timeout:   Subprocess timeout in seconds (default 600).

        Returns:
            ``{"success": bool, "output": str, "exit_code": int}``
        """
        # ── validate tool_name ──────────────────────────────────────
        if "/" in tool_name or "\\" in tool_name or ".." in tool_name:
            return {
                "success": False,
                "output": f"Invalid tool_name: {tool_name!r} (no path separators allowed)",
                "exit_code": -1,
            }

        script = tools_dir / f"{tool_name}.py"
        if not script.exists():
            available = sorted(p.stem for p in tools_dir.glob("*_cli.py"))
            return {
                "success": False,
                "output": (
                    f"Tool '{tool_name}' not found at {script}.\n"
                    f"Available tools: {', '.join(available) if available else '(none)'}"
                ),
                "exit_code": -1,
            }

        # ── build command ──────────────────────────────────────────
        cmd: list[str] = [sys.executable, str(script)]
        if cli_args.strip():
            cmd.extend(shlex.split(cli_args))

        logger.info("run_tool", tool_name=tool_name, cli_args=cli_args, cwd=str(tools_dir))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(tools_dir),
            )
            return {
                "success": result.returncode == 0,
                "output": result.stdout if result.returncode == 0 else result.stderr,
                "exit_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "output": f"Command timed out after {timeout} seconds.",
                "exit_code": -1,
            }
        except Exception as e:
            return {
                "success": False,
                "output": f"Error executing command: {e}",
                "exit_code": -1,
            }

    return run_tool


class CustomToolMiddleware(AgentMiddleware):
    """Middleware that exposes pipeline CLI tools as a single ``run_tool`` dispatch.

    Constructed per-node by the topology builder when a node has a
    ``tools/`` folder in the container image.  The agent calls
    ``run_tool(tool_name="seg_cli", args="--scene-duration 6.0")``
    and the middleware executes the corresponding script.
    """

    def __init__(self, tools_dir: Path) -> None:
        self.tools = [_build_run_tool(tools_dir)]
        logger.info(
            "custom_tool_middleware_initialized",
            tools_dir=str(tools_dir),
            tool_count=len(self.tools),
        )
