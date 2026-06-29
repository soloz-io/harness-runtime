"""GitBackend — pure cloner. Clones a git repo subfolder to a local temp directory.

Auth follows the openSWE proxy pattern: the GitHub token is fetched at runtime
from the control plane API (never passed as an env var to the pod), and injected
into git via a temporary GIT_ASKPASS script rather than embedded in the clone
URL. This keeps the token out of process listings and git error logs.

All file operations (read, ls, grep, glob) are handled by deepagents built-in
``FilesystemBackend`` and ``SkillsMiddleware``. This class does NOT implement
``BackendProtocol`` — it only performs the clone and exposes ``.path``.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import structlog

from .github_auth import get_github_token

logger = structlog.get_logger(__name__)

ENV_OWNER = "HARNESS_GIT_OWNER"
ENV_REPO = "HARNESS_GIT_REPO"


class GitBackendError(Exception):
    pass


class GitBackend:
    """Clone a git repo subfolder and expose its local path.

    The token is fetched at runtime from the SDK API via ``get_github_token()``
    and injected into ``git clone`` through a temp GIT_ASKPASS script (never
    embedded in the clone URL). This matches the openSWE pattern: the sandbox
    pod never receives ``GITHUB_TOKEN`` as an env var.
    """

    def __init__(
        self,
        git_ref: str,
        *,
        owner: str | None = None,
        repo: str | None = None,
        token: str | None = None,
    ) -> None:
        self.git_ref = git_ref
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
            has_auth=_has_token,
        )

        # ── Prepare auth via GIT_ASKPASS (openSWE pattern) ──────────────
        # Instead of embedding the token in the clone URL (which leaks it
        # via `ps aux`), write a temp askpass script that git prompts for
        # credentials. The script is removed immediately after the clone.
        askpass_path = None
        env = os.environ.copy()
        if token:
            fd, askpass_path = tempfile.mkstemp(suffix=".sh", prefix="git-askpass-")
            with os.fdopen(fd, "w") as f:
                f.write("#!/bin/sh\n")
                f.write('case "$1" in\n')
                f.write('  *Username*) echo "x-access-token" ;;\n')
                f.write(f'  *Password*) echo "{token}" ;;\n')
                f.write('  *)           echo "x-access-token" ;;\n')
                f.write("esac\n")
            os.chmod(askpass_path, stat.S_IRUSR | stat.S_IXUSR)
            env["GIT_ASKPASS"] = askpass_path
            env["GIT_TERMINAL_PROMPT"] = "0"

        _t0 = time.monotonic()
        args = ["git", "clone", "--depth", "1", clone_url, str(workdir)]
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=120)
        _elapsed = time.monotonic() - _t0

        # Clean up askpass script immediately
        if askpass_path:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass

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
