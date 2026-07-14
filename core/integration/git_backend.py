"""GitBackend — pure cloner. Clones a git repo subfolder to a local temp directory.

Auth is handled by the Agent Vault sidecar MITM proxy. The harness container
has ``HTTPS_PROXY`` set (sourced from ``/shared/proxy.env`` at startup), and
Agent Vault injects the correct GitHub credentials for all outbound git HTTPS
traffic. ``GIT_TERMINAL_PROMPT=0`` is set so git fails immediately instead of
prompting, and a dummy credential helper is provided to satisfy git's auth
negotiation before the proxy rewrites the request.

All file operations (read, ls, grep, glob) are handled by deepagents built-in
``FilesystemBackend`` and ``SkillsMiddleware``. This class does NOT implement
``BackendProtocol`` — it only performs the clone and exposes ``.path``.
"""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

ENV_OWNER = "AGENTREGISTRY_GIT_OWNER"
ENV_REPO = "AGENTREGISTRY_GIT_REPO"


class GitBackendError(Exception):
    pass


class GitBackend:
    """Clone a git repo subfolder and expose its local path.

    Uses the Agent Vault MITM proxy for authentication: ``HTTPS_PROXY`` is
    already set in the container env (sourced from ``/shared/proxy.env``).
    ``GIT_TERMINAL_PROMPT=0`` prevents interactive prompts; a dummy
    ``x-access-token`` credential is passed so git completes its auth
    handshake and lets the proxy inject the real token.
    """

    def __init__(
        self,
        git_ref: str,
        *,
        owner: str | None = None,
        repo: str | None = None,
    ) -> None:
        self.git_ref = git_ref
        self._tmpdir = None

        owner = owner or os.environ.get(ENV_OWNER)
        repo = repo or os.environ.get(ENV_REPO)

        logger.info(
            "git_backend_resolve_env",
            owner=owner,
            repo=repo,
            git_ref=git_ref,
        )

        if not owner:
            raise GitBackendError(f"{ENV_OWNER} must be set")
        if not repo:
            raise GitBackendError(f"{ENV_REPO} must be set")

        clone_url = f"https://github.com/{owner}/{repo}.git"

        self._tmpdir = Path(tempfile.mkdtemp(prefix="gitbackend-"))
        workdir = self._tmpdir / "repo"

        logger.info(
            "git_backend_clone_cmd",
            repo=f"{owner}/{repo}",
            git_ref=git_ref,
            tmpdir=str(self._tmpdir),
        )

        # ── Auth via Agent Vault proxy ──────────────────────────────
        # HTTPS_PROXY is sourced from /shared/proxy.env before uvicorn starts,
        # so it's already in os.environ. We set GIT_TERMINAL_PROMPT=0 so git
        # never blocks waiting for TTY input, and embed a dummy placeholder
        # credential in the URL so git's auth handshake completes. The MITM
        # proxy rewrites the Authorization header with the real GitHub token.
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"

        # In local dev, use the git token from .env if available.
        # In production, Agent Vault proxy rewrites the placeholder.
        git_token = os.environ.get("AGENTREGISTRY_GITHUB_TOKEN", "placeholder")
        clone_url_with_cred = clone_url.replace("https://", f"https://x-access-token:{git_token}@")

        _t0 = time.monotonic()
        args = ["git", "clone", "--depth", "1", clone_url_with_cred, str(workdir)]
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
        _elapsed = time.monotonic() - _t0

        logger.info(
            "git_backend_clone_done",
            returncode=result.returncode,
            elapsed_ms=round(_elapsed * 1000),
            ok=result.returncode == 0,
            stderr=result.stderr[:1000] if result.stderr else None,
            stdout=result.stdout[:500] if result.stdout else None,
        )

        if result.returncode != 0:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            raise GitBackendError(
                f"git clone failed (exit {result.returncode}): {result.stderr.strip()}"
            )

        sub = workdir / git_ref
        if not sub.exists():
            _entries = [str(p.relative_to(workdir)) for p in workdir.rglob("*")][:50]
            logger.warning(
                "git_backend_subfolder_missing_creating_empty",
                git_ref=git_ref,
                repo_contents=_entries,
            )
            sub.mkdir(parents=True, exist_ok=True)
        elif not sub.is_dir():
            logger.warning(
                "git_backend_subfolder_not_dir_removing_and_recreating",
                git_ref=git_ref,
            )
            shutil.rmtree(str(sub))
            sub.mkdir(parents=True, exist_ok=True)

        logger.info(
            "git_backend_navigate_subfolder",
            subfolder=str(sub),
            exists=True,
            is_dir=True,
        )

        self.path = sub.resolve()
        self.repo_path = workdir.resolve()

        _files = [
            str(p.relative_to(self.path)) for p in sorted(self.path.rglob("*")) if p.is_file()
        ][:30]
        logger.info(
            "git_backend_ready",
            git_ref=git_ref,
            path=str(self.path),
            file_count=len(_files),
            sample_files=_files[:10],
        )

    def cleanup(self) -> None:
        """Remove the cloned temp directory."""
        if getattr(self, "_tmpdir", None) and self._tmpdir.exists():
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            logger.debug("git_backend_cleanup", path=str(self._tmpdir))

    def __del__(self) -> None:
        self.cleanup()
