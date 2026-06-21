"""Shared fixtures for harness-runtime HTTP integration tests.

The sse_server fixture starts a uvicorn subprocess per module
(module-scoped), serving the FastAPI app on a dedicated port.
All integration tests should use this instead of the legacy
CLI subprocess approach.
"""

from __future__ import annotations

import httpx
import pytest
import subprocess
import sys
import time
from pathlib import Path

_HTTP_PORT = 9876
BASE_URL = f"http://127.0.0.1:{_HTTP_PORT}"


@pytest.fixture(scope="module")
def sse_server() -> None:
    """Start the HTTP server as a subprocess on port 9876.

    Uses cli.py which starts Redis + uvicorn.  Module-scoped so
    all tests in a file share one server instance.
    """
    cli_path = Path(__file__).parent.parent.parent / "cli.py"

    proc = subprocess.Popen(
        [sys.executable, str(cli_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **__import__("os").environ,
            "PYTHONUNBUFFERED": "1",
            "PORT": str(_HTTP_PORT),
        },
    )

    for _ in range(200):
        if proc.poll() is not None:
            pytest.fail(f"Server exited prematurely (code {proc.returncode})")
        try:
            resp = httpx.get(f"{BASE_URL}/health", timeout=2.0)
            if resp.status_code == 200:
                break
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("SSE server did not start within 20s")

    yield

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
