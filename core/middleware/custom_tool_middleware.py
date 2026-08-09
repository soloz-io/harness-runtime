"""Custom Tool Middleware — provides a generic ``run_tool`` dispatch to agents.

When an agent node has a ``tools/`` folder baked into the container image,
``ToolsManager`` discovers it and ``CustomToolMiddleware`` exposes a single
``run_tool`` tool that the LLM calls to execute any CLI script in that folder.

The tool runs ``python <tools_dir>/<tool_name>.py <args>`` via subprocess and
returns a structured result.  The agent never sees filesystem paths or manages
dependencies — only the tool name and CLI flags.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool as lc_tool

logger = structlog.get_logger(__name__)


def _scope_env() -> dict[str, str]:
    """Read session scope ids from the active graph config for the subprocess env.

    ``run_tool`` executes inside the graph, so ``get_config()`` exposes the
    ``configurable`` keys set by the executor (``workspace_id``, ``app_id``).
    The CLI subprocess inherits these so its DB reads can resolve the correct
    ``agent_output_files`` scope key.  Falls back to ``os.environ`` when not
    running inside a graph context.
    """
    extra: dict[str, str] = {}
    try:
        from langgraph.config import get_config

        config = get_config()
    except RuntimeError:
        config = None
    configurable = (config or {}).get("configurable") or {}
    for key in ("WORKSPACE_ID", "APP_ID", "SESSION_ID"):
        value = configurable.get(key.lower())
        if value is not None:
            extra[key] = str(value)
    return extra


def _build_run_tool(tools_dirs: list[Path]):
    """Factory: create a ``run_tool`` closure bound to *tools_dirs*.

    Scripts are resolved against the node's ``tools/`` folder first, then the
    app-wide ``shared/tools/`` folder.  The working directory is the folder
    that contained the script so relative imports (``workdir``, ``services``)
    resolve correctly.
    """

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

        # ── resolve script (node dir first, then shared dir) ────────
        script: Path | None = None
        script_dir: Path | None = None
        for d in tools_dirs:
            candidate = d / f"{tool_name}.py"
            if candidate.exists():
                script = candidate
                script_dir = d
                break

        if script is None or script_dir is None:
            available = sorted(p.stem for d in tools_dirs for p in d.glob("*_cli.py"))
            return {
                "success": False,
                "output": (
                    f"Tool '{tool_name}' not found.\n"
                    f"Available tools: {', '.join(available) if available else '(none)'}"
                ),
                "exit_code": -1,
            }

        # ── build command ──────────────────────────────────────────
        cmd: list[str] = [sys.executable, str(script)]
        if cli_args.strip():
            cmd.extend(shlex.split(cli_args))

        logger.info(
            "run_tool",
            tool_name=tool_name,
            cli_args=cli_args,
            cwd=str(script_dir),
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(script_dir),
                env={**os.environ, **_scope_env()},
            )
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            combined = f"{out}\n{err}".strip() if out and err else (out or err or "")
            return {
                "success": result.returncode == 0,
                "output": combined,
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

    Constructed per-node by the topology builder when a node has access to
    ``tools/`` folders in the container image (own folder plus the app-wide
    ``shared/tools/``).  The agent calls
    ``run_tool(tool_name="seg_cli", args="--scene-duration 6.0")``
    and the middleware executes the corresponding script.
    """

    def __init__(self, tools_dirs: list[Path]) -> None:
        self.tools = [_build_run_tool(tools_dirs)]
        logger.info(
            "custom_tool_middleware_initialized",
            tools_dirs=[str(d) for d in tools_dirs],
            tool_count=len(self.tools),
        )
