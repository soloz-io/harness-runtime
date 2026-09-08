"""
Metro/HMR — the ADR-033 §6.1 checkpoint trigger, ported from opencode's
``opencode_event_bridge``.

- ``supervisor``: starts/supervises the Metro (``npx expo start --web``)
  subprocess once the agent has scaffolded an Expo project
- ``watcher``: an outbound client connection to Metro's own ``/hot``
  socket, watching for the ``update-done`` HMR frame — not a route
  anything connects to (see ``watcher.py``'s docstring for why)
- ``checkpoint_trigger``: debounces bursts of HMR updates into one
  HMR boundary hook (no longer persists anything — see checkpoint_trigger)
- ``config``: ``METRO_URL``/``METRO_PORT``/``CHECKPOINT_DEBOUNCE_SECONDS``

Call sites (``core/session/session.py``, ``core/topology/
composite_topology.py``) only ever import ``ensure_metro_running`` and
``ensure_watcher_running`` from here.
"""

from core.metro.supervisor import ensure_metro_running
from core.metro.watcher import ensure_watcher_running

__all__ = ["ensure_metro_running", "ensure_watcher_running"]
