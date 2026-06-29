"""
GitHub Middleware — provides Git and GitHub interaction tools to agents.

Exposes `execute_shell` and `open_pull_request` for skill maintenance and git operations.
"""

import subprocess

import httpx
import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool

from ..integration.github_auth import get_github_token

logger = structlog.get_logger(__name__)


@tool
def execute_shell(command: str) -> str:
    """Execute a shell command. Use this for git clone, git commit, git push, and search tools like rg."""
    logger.info("execute_shell", command=command)
    try:
        # We use a 300s timeout by default, similar to open-swe
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return f"Command failed with exit code {result.returncode}:\n{result.stderr}"
        return result.stdout
    except subprocess.TimeoutExpired:
        return "Command timed out after 300 seconds."
    except Exception as e:
        return f"Error executing command: {e}"


@tool
def open_pull_request(owner: str, repo: str, head: str, base: str, title: str, body: str) -> str:
    """Open a pull request on GitHub via the REST API."""
    logger.info("open_pull_request", owner=owner, repo=repo, head=head, base=base, title=title)

    token = get_github_token()
    if not token:
        return "Failed to open PR: GitHub token not available (WAYPOINT_SDK_BASE_URL or resolution failed)"

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
    }
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                return f"Failed to open PR: HTTP {resp.status_code}\n{resp.text}"
            data = resp.json()
            return f"Pull Request successfully opened: {data.get('html_url')}"
    except Exception as e:
        return f"Error opening PR: {e}"


class GitHubMiddleware(AgentMiddleware):
    """Provides shell execution and github tools to agents.

    Wire this middleware into the agent's middleware stack when `"execute_shell"`
    or `"open_pull_request"` are requested in the configuration.
    """

    tools = [execute_shell, open_pull_request]
