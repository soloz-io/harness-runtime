"""
Which filesystem backend a session gets based on ``persistent_workspace``.

The agent definition's top-level ``persistent_workspace`` field determines
the backend:

- ``"db"`` — ``DBBackend``: DB-backed, ephemeral, no real disk. Cross-session
  file visibility via PostgreSQL queries.
- ``"s3"`` — ``S3Backend``: Real-disk + shell execution. S3 persistence is
  handled by the platform's workspace-sync sidecar (ADR-036 §10), not by
  this backend.

This is a BACKEND choice and nothing more. Whether those files outlive the pod —
a volume, an object store, neither — is the platform's concern and is not
observable from this process (ADR-036 §10).
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def resolve_backend_mode(agent_definition: Dict[str, Any]) -> str:
    """Return the persistent_workspace mode from the agent definition.

    Always returns a valid mode string — defaults to ``"db"`` when the
    key is missing or invalid (defensive; callers should validate).
    """
    mode = agent_definition.get("persistent_workspace", "db")
    if mode not in ("db", "s3"):
        return "db"
    return mode


def is_s3_mode(agent_definition: Dict[str, Any]) -> bool:
    """True when the agent definition requests S3-backed persistence."""
    return resolve_backend_mode(agent_definition) == "s3"


def resolve_backend(
    agent_definition: Dict[str, Any],
    workspace_id: str,
    session_id: str,
    db_pool: Any,
    app_id: Optional[str] = None,
) -> Any:
    """Build the appropriate backend based on ``persistent_workspace``.

    Returns:
        ``DBBackend`` for ``"db"`` mode, or ``LocalShellBackend`` (via
        ``build_s3_backend``) for ``"s3"`` mode.
    """
    from core.session.backends import build_db_backend, build_s3_backend

    mode = resolve_backend_mode(agent_definition)
    if mode == "s3":
        return build_s3_backend()
    return build_db_backend(workspace_id, session_id, db_pool, app_id=app_id)
