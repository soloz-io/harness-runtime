"""
GitHub Middleware — provides Git and GitHub interaction tools to agents.

Exposes `execute_shell` and `open_pull_request` for skill maintenance and git operations.
"""

import subprocess

import httpx
import structlog
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool

logger = structlog.get_logger(__name__)


@tool
def execute_shell(command: str, timeout: int = 300) -> dict:
    """Execute a shell command. Use this for git clone, git commit, git push, and search tools like rg.

    Args:
        command: Shell command to run.
        timeout: Timeout in seconds. Defaults to 300.

    Returns:
        On success: {"success": true, "output": str, "exit_code": int, "truncated": false}
        On failure: {"success": false, "output": str, "exit_code": int, "truncated": false}
        On timeout: {"success": false, "output": "Command timed out after <N> seconds.", "exit_code": -1, "truncated": true}
    """
    logger.info("execute_shell", command=command, timeout=timeout)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
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
def open_pull_request(
    owner: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
    draft: bool = True,
) -> dict:
    """Open a GitHub pull request via the REST API.

    Authentication is handled transparently by the Agent Vault sidecar
    proxy. The request to ``api.github.com`` is routed through the proxy,
    which injects the correct bearer token. No token is fetched or stored
    in the harness process.

    Use this to OPEN a NEW pull request. Push your branch with
    `git push origin <branch>` BEFORE calling this tool. For everything else —
    updating an existing PR, marking it ready for review — use `GH_TOKEN=dummy gh pr edit`
    via `execute_shell`. If a PR already exists for the branch, this returns
    that PR's URL without creating a duplicate.

    Args:
        owner: Repository owner/org (e.g. "soloz-io").
        repo: Repository name (e.g. "agentregistry").
        head: The branch with your changes (must already be pushed to origin).
        base: The branch to merge into (e.g. "main").
        title: PR title.
        body: PR description (Markdown).
        draft: Open as a draft PR. Defaults to True.

    Returns:
        On success: {"success": true, "created": bool, "url": str, "number": int}
            ``created`` is False when an open PR already existed.
        On failure: {"success": false, "error": str}
    """
    logger.info(
        "open_pull_request", owner=owner, repo=repo, head=head, base=base, title=title, draft=draft
    )

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
        "draft": draft,
    }
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(api_url, headers=headers, json=payload)

            # A PR for this head branch may already exist — return existing URL
            if resp.status_code == 422:
                existing = _find_existing_pr(client, headers, owner, repo, head)
                if existing is not None:
                    return {
                        "success": True,
                        "created": False,
                        "url": existing.get("html_url"),
                        "number": existing.get("number"),
                    }
                return {
                    "success": False,
                    "error": f"Failed to open PR: HTTP {resp.status_code}\n{resp.text}",
                }

            if resp.status_code >= 400:
                return {
                    "success": False,
                    "error": f"Failed to open PR: HTTP {resp.status_code}\n{resp.text}",
                }

            data = resp.json()
            return {
                "success": True,
                "created": True,
                "url": data.get("html_url"),
                "number": data.get("number"),
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Error opening PR: {e}",
        }


def _find_existing_pr(
    client: httpx.Client,
    headers: dict[str, str],
    owner: str,
    repo: str,
    head: str,
) -> dict | None:
    """Check if an open PR already exists for the given head branch."""
    try:
        resp = client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=headers,
            params={"head": f"{owner}:{head}", "state": "open"},
        )
        if resp.status_code != 200:
            return None
        items = resp.json()
        if isinstance(items, list) and items:
            return items[0]
    except Exception:
        logger.warning("find_existing_pr_failed", owner=owner, repo=repo, head=head)
    return None


class GitHubMiddleware(AgentMiddleware):
    """Provides shell execution and github tools to agents.

    Wire this middleware into the agent's middleware stack when `"execute_shell"`
    or `"open_pull_request"` are requested in the configuration.
    """

    tools = [execute_shell, open_pull_request]
