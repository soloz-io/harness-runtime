from typing import Any, Optional


def build_db_backend(
    workspace_id: str,
    session_id: str,
    pool: Any,
    app_id: Optional[str] = None,
) -> Optional[Any]:
    """Build a DBBackend if a DB pool is available.

    The single backend serves both namespaces: the session workspace keyed
    by ``workspace_id`` and — when ``app_id`` is provided — app-global
    ``.global/`` artifacts keyed by ``app_id``.

    Returns ``None`` when the ``deepagents`` package or DB pool is
    unavailable — callers must handle that case.
    """
    if pool is None:
        return None
    try:
        from core.workspace.backends.db import DBBackend

        return DBBackend(
            workspace_id=workspace_id,
            session_id=session_id,
            pool=pool,
            app_id=app_id,
        )
    except ImportError:
        return None


def build_s3_backend(root_dir: str = "/") -> Any:
    """Build a real-disk backend for S3-backed persistence.

    ``root_dir`` is the filesystem root (``"/"``), not ``"/workspace"``. Every
    other path convention here treats ``/workspace/...`` as a real absolute
    path — the DB backend's path handling, ``shell_middleware``'s SKILLS_BASE,
    ``core.tools.embedded_loader``'s WORKSPACE_ROOT, and critically
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
