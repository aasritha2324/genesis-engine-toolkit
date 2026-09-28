"""Shared fixtures for the AdVanta test suite."""
import os
import sys
from pathlib import Path

import pytest

BASE_URL = os.environ.get("ADVANTA_BASE_URL", "http://localhost:8080")

# Make the aggregator's pure module importable for unit tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aggregator"))


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def http():
    import requests

    session = requests.Session()
    session.headers["Content-Type"] = "application/json"
    return session
