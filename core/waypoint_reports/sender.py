"""
Reports from a sandbox to Waypoint.

Some things happen only inside the sandbox -- the agent asks the user a
question, the workspace changes -- and a client following the session without
watching its turn can learn of them only if Waypoint publishes them. The harness
reports each to the SDK, which publishes a session entry for it (waypoint
ADR-025, amendment 2026-10-02).

A report names no session: Waypoint takes the session from the sandbox's
identity, so a sandbox can only report on its own. No secret is read; the
sandbox presents its platform-issued identity token, a file the platform mounts
and renews.

A report is a hint and never holds up the turn: it is sent on a background
thread, and a failure is logged, not raised.
"""

import os
import threading
from pathlib import Path

import httpx
import structlog

logger = structlog.get_logger(__name__)

IDENTITY_TOKEN_PATH = Path("/var/run/secrets/platform.soloz.io/identity/token")
IDENTITY_HEADER = "x-waypoint-sandbox-identity"
TIMEOUT_SECONDS = 10.0


def _send(route: str, session_id: str, sdk_url: str, token: str) -> None:
    try:
        resp = httpx.post(
            f"{sdk_url}{route}", headers={IDENTITY_HEADER: token}, timeout=TIMEOUT_SECONDS
        )
        if resp.status_code != 202:
            logger.warning(
                "waypoint_report_refused",
                route=route,
                session_id=session_id,
                status=resp.status_code,
                body=resp.text[:300],
            )
    except httpx.HTTPError as e:
        logger.warning(
            "waypoint_report_failed",
            route=route,
            session_id=session_id,
            error=f"{type(e).__name__}: {e}",
        )


def post_as_sandbox(route: str, session_id: str) -> None:
    """Report to Waypoint on ``route`` as this sandbox, without waiting for it.

    Outside a sandbox -- no SDK address or no identity token -- there is no
    Waypoint session to report to, and nothing is sent.
    """
    sdk_url = os.environ.get("WAYPOINT_SDK_BASE_URL", "").rstrip("/")
    if not sdk_url or not IDENTITY_TOKEN_PATH.is_file():
        logger.debug("waypoint_report_skipped_not_a_sandbox", route=route, session_id=session_id)
        return
    token = IDENTITY_TOKEN_PATH.read_text().strip()
    if not token:
        logger.warning(
            "waypoint_report_skipped_empty_identity_token", route=route, session_id=session_id
        )
        return
    threading.Thread(target=_send, args=(route, session_id, sdk_url, token), daemon=True).start()
