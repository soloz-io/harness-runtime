from typing import Any, Optional


def build_artifact_backend(
    workspace_id: str,
    session_id: str,
    pool: Any,
    app_id: Optional[str] = None,
) -> Optional[Any]:
    """Build a SessionArtifactBackend if a DB pool is available.

    The single backend serves both namespaces: the session workspace keyed
    by ``workspace_id`` and — when ``app_id`` is provided — app-global
    ``.global/`` artifacts keyed by ``app_id``.

    Returns ``None`` when the ``deepagents`` package or DB pool is
    unavailable — callers must handle that case.
    """
    if pool is None:
        return None
    try:
        from core.backends.artifact import SessionArtifactBackend

        return SessionArtifactBackend(
            workspace_id=workspace_id,
            session_id=session_id,
            pool=pool,
            app_id=app_id,
        )
    except ImportError:
        return None
