import os

import httpx
import structlog

logger = structlog.get_logger(__name__)

SDK_AUTH_GITHUB_TOKEN_PATH = "/internal/auth/github/token"

_token_cache: str | None = None


def get_github_token() -> str | None:
    """Fetch the multi-tenant GitHub token dynamically from the SDK."""
    global _token_cache
    if _token_cache is not None:
        return _token_cache

    sdk_base_url = os.environ.get("WAYPOINT_SDK_BASE_URL")
    if not sdk_base_url:
        logger.warning("WAYPOINT_SDK_BASE_URL is not set, cannot fetch github token")
        return None

    headers = {"Content-Type": "application/json"}
    internal_token = os.environ.get("WAYPOINT_INTERNAL_TOKEN")
    if internal_token:
        headers["x-waypoint-internal-token"] = internal_token

    url = f"{sdk_base_url.rstrip('/')}{SDK_AUTH_GITHUB_TOKEN_PATH}"
    try:
        # 10s timeout should be plenty for an internal network call
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            token = data.get("token")
            if token:
                _token_cache = token
                logger.info("github_token_resolved", source="sdk_api")
                return token
            logger.warning("github_token_resolved", source="sdk_api", token="empty")
    except Exception as e:
        logger.error("Failed to fetch github token from SDK", error=str(e))

    return None
