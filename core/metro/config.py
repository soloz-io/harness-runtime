"""
Metro/HMR configuration — ported from opencode's
``opencode_event_bridge/config.py``.
"""

import os

METRO_PORT = os.getenv("METRO_PORT", "8081")
METRO_URL = os.getenv("METRO_URL", f"ws://127.0.0.1:{METRO_PORT}")

# ADR-033 §6.1: "1.5s trailing debounce is a starting default, not yet
# validated against real multi-file-save agent turns." Unchanged here.
CHECKPOINT_DEBOUNCE_SECONDS = float(os.getenv("CHECKPOINT_DEBOUNCE_SECONDS", "1.5"))

try:
    import websockets  # noqa: F401

    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
