"""
Active workspace context for this pod.

Sandbox pods are per-session (``sandboxName(sessionId)``), so a given pod
only ever serves one session/workspace at a time. Set once in
``Session.__init__`` — which means nothing here is populated until the first
turn starts, and readers that can run earlier must say what they do about
that (see ``get_active_app_id``). Read by code that otherwise has no natural
way to receive these values:

- ``workspace_id``: the (possibly app-scoped) workspace key — read by
  ``core/metro/watcher.py``'s HMR handler to know which workspace a
  detected Metro update belongs to.
- ``app_id``/``session_id``: real identity (distinct from the opaque
  ``workspace_id``) needed for the ``app_workspace_checkpoints`` row —
  read by the checkpoint recorder after a successful checkpoint.
- ``pool``: the DB connection pool, for the same checkpoint-recording call.
"""

import os
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
    """The app this pod serves.

    Falls back to ``APP_ID`` from the environment, which is NOT a guess: the
    pod is created per session with that variable baked into its spec, so it
    is the same fact from a source that exists at process start rather than
    only once a turn has begun.

    The fallback is load-bearing. ``set_active_context`` runs in
    ``Session.__init__``, but Metro is supervised from the HTTP path, so the
    preview document is served the moment the canvas frames it — observed
    eleven seconds BEFORE ``session_initialized`` on a freshly recycled
    sandbox. The WebSocket appId patch is emitted into that document only
    when this returns a value, and a document served without it can never
    route its own ``/hot`` socket: BFF destroys the upgrade, and Fast Refresh
    is dead for the life of that document, with a manual reload the only way
    to see a change. Reading the environment removes the ordering dependency
    entirely.
    """
    return _app_id or os.environ.get("APP_ID") or None


def get_active_session_id() -> Optional[str]:
    return _session_id


def get_active_pool() -> Any:
    return _pool
