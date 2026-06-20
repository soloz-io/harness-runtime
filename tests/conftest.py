"""Shared test fixtures and artifact capture."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

FAKE_SERVER = Path(__file__).parent / "fake_server.py"
MOCK_DATA = Path(__file__).parent / "mock" / "simple-bug-fix-invoke-requests.json"


@pytest.fixture()
def fake_server_command() -> list[str]:
    """Command to run the fake LiteLLM NDJSON echo server."""
    return [sys.executable, str(FAKE_SERVER)]


@pytest.fixture()
def request_payloads() -> dict:
    """Load the real SDK request payloads from mock data."""
    return json.loads(MOCK_DATA.read_text())


# ---------------------------------------------------------------------------
# Artifact capture: save subprocess stderr + stdout per test
# ---------------------------------------------------------------------------

_LOG_DIR = Path(__file__).parent.parent / "log" / "runs"


@pytest.fixture(scope="session", autouse=True)
def _create_run_dir() -> Path:
    """Create a timestamped run directory for artifacts."""
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_dir = _LOG_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    # Write metadata
    meta = {
        "timestamp": ts,
        "python": sys.version,
        "platform": sys.platform,
        "argv": sys.argv,
    }
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2))
    return run_dir


@pytest.fixture()
def artifact_dir(_create_run_dir: Path, request: pytest.FixtureRequest) -> Path:
    """Per-test subdirectory for captured artifacts."""
    test_name = request.node.name
    test_dir = _create_run_dir / test_name
    test_dir.mkdir(exist_ok=True)
    return test_dir
