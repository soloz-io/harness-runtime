"""Authorisation of calls the waypoint SDK makes INTO this sandbox.

The SDK proves itself with ``x-waypoint-internal-token``, its own ``/internal/*``
convention. This process only has to CHECK that token, never present it, so it
is given the token's SHA-256 (``WAYPOINT_INTERNAL_TOKEN_SHA256``) and not the
token itself.

That is the whole point (zero-ops ADR-052 §19.6: no secret-valued environment
variable in the workload container). Everything in this container -- the agent,
its tools, anything it runs -- can read this process's environment. A digest
lets it verify a caller without holding anything a caller could replay; the
token stayed in the environment before only because the check was written as a
string comparison.

Outbound calls from the sandbox to the SDK do not use this module: they carry no
credential at all, and the agent-vault proxy injects it on the way out.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional


def is_internal_caller(token: Optional[str]) -> bool:
    if os.environ.get("WAYPOINT_ENV") == "local":
        return True
    expected = os.environ.get("WAYPOINT_INTERNAL_TOKEN_SHA256", "").strip().lower()
    if not expected:
        # Matches the SDK's own isInternalAuthorized: no configured secret is
        # tolerated only outside production.
        return os.environ.get("WAYPOINT_ENV") != "production"
    if not token:
        return False
    presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(presented, expected)


# Loopback is reachable only from inside this pod: the agent's own tools (a
# bootstrap script asking for Metro, say) and nothing else. They share this
# process's trust boundary already -- they could read its environment -- so
# they are let in without a credential rather than handed one. The SDK never
# arrives on loopback; it reaches the pod's own address.
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def is_in_pod_caller(client_host: Optional[str]) -> bool:
    return (client_host or "") in _LOOPBACK
