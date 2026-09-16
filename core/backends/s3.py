"""S3Backend: Real-disk backend with sidecar-managed S3 persistence.

When ``persistent_workspace: "s3"`` is set in the agent definition, the
session uses this backend. It wraps deepagents' ``LocalShellBackend`` for
real-disk file operations and shell execution.

S3 persistence itself is handled by the platform's workspace-sync sidecar
(zero-ops ADR-052 §14), not by this backend. The sidecar snapshots the
workspace to S3 on its own schedule and at teardown. This backend simply
provides the real-disk access that makes sidecar persistence meaningful.
"""

from __future__ import annotations

from typing import Any


def build_s3_backend(root_dir: str = "/") -> Any:
    """Build a real-disk backend for S3-backed persistence.

    ``root_dir`` is the filesystem root (``"/"``), not ``"/workspace"``. Every
    other path convention here treats ``/workspace/...`` as a real absolute
    path — the DB backend's path handling, ``shell_middleware``'s SKILLS_BASE,
    ``embedded_tool_loader``'s WORKSPACE_ROOT, and critically
    ``subagent_builder``'s FilesystemPermission ACLs (``paths=["/workspace/**"]``).
    With ``virtual_mode=True`` resolving as ``root_dir / path``, a root of
    ``/workspace`` would make the agent's own ``/workspace/...`` calls resolve to
    ``/workspace/workspace/...`` — confirmed against a real pod, where
    ``ls("/workspace")`` and skill loading both failed with ``path_not_found``.

    A root of ``/`` forgoes ``virtual_mode``'s traversal guard, which is moot
    once everything is trivially under the root; the real containment for this
    specialist is the ACL layer in ``subagent_builder``, not this guard.
    """
    from deepagents.backends.local_shell import LocalShellBackend

    return LocalShellBackend(root_dir=root_dir, virtual_mode=True)
