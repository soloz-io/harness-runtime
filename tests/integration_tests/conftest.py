"""Shared fixtures for harness-runtime integration tests.

The harness fixture starts a CLI subprocess per test (function-scoped),
matching how the Waypoint SDK spawns the runtime.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture(scope="function")
def harness(artifact_dir: Path) -> subprocess.Popen[bytes]:
    """Start the CLI subprocess once per test, saving artifacts."""
    cli_path = Path(__file__).parent.parent.parent / "cli.py"
    if not cli_path.exists():
        import shutil
        installed = shutil.which("harness-runtime")
        if not installed:
            pytest.fail(
                "harness-runtime not found. Install with: pip install -e ."
            )
        cli_path = Path(installed)

    log_dir = Path(__file__).parent.parent / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, str(cli_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "HARNESS_LOG_FILE": str(log_dir / "harness-cli.log"),
            "HARNESS_LOG_LEVEL": "DEBUG",
        },
    )

    for _ in range(100):
        if proc.poll() is not None:
            pytest.fail(f"CLI exited prematurely (code {proc.returncode})")
        time.sleep(0.1)

    yield proc

    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    if proc.stderr:
        stderr = proc.stderr.read()
        if stderr:
            (artifact_dir / "stderr.log").write_bytes(stderr)
