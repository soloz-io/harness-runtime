"""GitBackend — pure cloner. Clones a git repo subfolder to a local temp directory.

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

from .github_auth import get_github_token

logger = structlog.get_logger(__name__)

ENV_OWNER = "HARNESS_GIT_OWNER"
ENV_REPO = "HARNESS_GIT_REPO"
DEFAULT_BRANCH = "main"


class GitBackendError(Exception):
    pass


class GitBackend:
    """Clone a git repo subfolder and expose its local path.

    Environment variables:

    - ``HARNESS_GIT_OWNER`` (required) — GitHub org or user
    - ``HARNESS_GIT_REPO`` (required) — GitHub repository name
    - ``GITHUB_TOKEN`` (optional) — token for private repos
    """

    def __init__(
        self,
        git_ref: str,
        *,
        owner: str | None = None,
        repo: str | None = None,
        token: str | None = None,
        branch: str = DEFAULT_BRANCH,
    ) -> None:
        self.git_ref = git_ref
        self.branch = branch
        self._tmpdir = None

        owner = owner or os.environ.get(ENV_OWNER)
        repo = repo or os.environ.get(ENV_REPO)
        token = token or get_github_token()

        _has_token = token is not None
        logger.info(
            "git_backend_resolve_env",
            owner=owner,
            repo=repo,
            has_token=_has_token,
            branch=branch,
            git_ref=git_ref,
        )

        if not owner:
            raise GitBackendError(f"{ENV_OWNER} must be set")
        if not repo:
            raise GitBackendError(f"{ENV_REPO} must be set")

        clone_url = f"https://github.com/{owner}/{repo}.git"
        if token:
            clone_url = f"https://{token}@github.com/{owner}/{repo}.git"

        self._tmpdir = Path(tempfile.mkdtemp(prefix="gitbackend-"))
        workdir = self._tmpdir / "repo"

        _masked_url = f"https://{owner}/{repo}.git"
        logger.info(
            "git_backend_clone_cmd",
            repo=f"{owner}/{repo}",
            branch=branch,
            git_ref=git_ref,
            tmpdir=str(self._tmpdir),
            has_auth=_has_token,
        )

        _t0 = time.monotonic()
        args = ["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
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
        logger.info(
            "git_backend_navigate_subfolder",
            subfolder=str(sub),
            exists=sub.exists(),
            is_dir=sub.is_dir() if sub.exists() else None,
        )

        if not sub.exists():
            _entries = [str(p.relative_to(workdir)) for p in workdir.rglob("*")][:50]
            logger.error(
                "git_backend_subfolder_missing",
                git_ref=git_ref,
                repo_contents=_entries,
            )
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            raise GitBackendError(
                f"subfolder '{git_ref}' not found in cloned repo. "
                f"Repo root has {len(_entries)} visible entries."
            )
        if not sub.is_dir():
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            raise GitBackendError(f"'{git_ref}' is not a directory")

        self.path = sub.resolve()

        _files = [
            str(p.relative_to(self.path)) for p in sorted(self.path.rglob("*")) if p.is_file()
        ][:30]
        logger.info(
            "git_backend_ready",
            git_ref=git_ref,
            branch=branch,
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
