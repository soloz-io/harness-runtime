from typing import Any, Optional


def build_artifact_backend(
    workspace_id: str,
    session_id: str,
    pool: Any,
) -> Optional[Any]:
    """Build an ArtifactBackend if a DB pool is available.

    Returns ``None`` when the ``deepagents`` package or DB pool is
    unavailable — callers must handle that case.
    """
    if pool is None:
        return None
    try:
        from core.backends.artifact import ArtifactBackend

        return ArtifactBackend(
            workspace_id=workspace_id,
            session_id=session_id,
            pool=pool,
        )
    except ImportError:
        return None
