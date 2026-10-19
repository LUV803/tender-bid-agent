"""Keep the offline test suite away from any real MinIO server."""

from __future__ import annotations

import os

import pytest

# ``config`` loads the developer's .env at import time.  Set the override
# first so tests never attempt a network request even when MinIO is enabled
# locally.  Storage-specific tests opt back in with ``make_settings``.
os.environ["MINIO_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def _reset_storage_circuit():
    """A simulated outage must not leak its cooldown into later tests."""
    from bidding import storage

    storage._unavailable_until = 0.0
    yield
    storage._unavailable_until = 0.0
