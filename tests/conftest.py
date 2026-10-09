"""Pytest configuration and shared fixtures for test suite."""

import os
import sys
import tempfile
import pytest

# Ensure docker/mcp-service is on Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "docker", "mcp-service")))

from src.config import Settings
from src.ledger import IdempotencyLedger


@pytest.fixture
def temp_ledger_file():
    """Create a temporary ledger file and clean it up after test."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def ledger(temp_ledger_file):
    """Instantiate a fresh IdempotencyLedger."""
    return IdempotencyLedger(temp_ledger_file)


@pytest.fixture
def mock_settings(temp_ledger_file, tmp_path):
    """Provide configured settings for testing."""
    return Settings(
        session_secret="test-session-secret",
        data_dir=str(tmp_path),
        idempotency_ledger_path=temp_ledger_file,
    )
