"""
Active workspace context for this pod.

Sandbox pods are per-session (``sandboxName(sessionId)``), so a given pod
only ever serves one session/workspace at a time. Set once in
``Session.__init__`` and read by code that otherwise has no natural way to
receive these values:

- ``workspace_id``: the (possibly app-scoped) workspace key — read by
  ``core/metro/watcher.py``'s HMR handler to know which workspace a
  detected Metro update belongs to.
- ``app_id``/``session_id``: real identity (distinct from the opaque
  ``workspace_id``) needed for the ``app_workspace_checkpoints`` row —
  read by the checkpoint recorder after a successful checkpoint.
- ``pool``: the DB connection pool, for the same checkpoint-recording call.
"""

from typing import Any, Optional

_workspace_id: Optional[str] = None
_app_id: Optional[str] = None
_session_id: Optional[str] = None
_pool: Any = None


def set_active_context(
    workspace_id: str, app_id: Optional[str], session_id: str, pool: Any
) -> None:
    global _workspace_id, _app_id, _session_id, _pool
    _workspace_id = workspace_id
    _app_id = app_id
    _session_id = session_id
    _pool = pool


def get_active_workspace_id() -> Optional[str]:
    return _workspace_id


def get_active_app_id() -> Optional[str]:
    return _app_id


def get_active_session_id() -> Optional[str]:
    return _session_id


def get_active_pool() -> Any:
    return _pool
