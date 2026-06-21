"""Top-level fixtures for harness-runtime integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def artifact_dir(tmp_path: Path) -> Path:
    """Per-test temporary directory for captured artifacts."""
    return tmp_path
