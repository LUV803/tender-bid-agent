"""Keep the offline test suite away from any real MinIO server."""

from __future__ import annotations

import os

# ``config`` loads the developer's .env at import time.  Set the override
# first so tests never attempt a network request even when MinIO is enabled
# locally.  Storage-specific tests opt back in with ``make_settings``.
os.environ["MINIO_ENABLED"] = "false"
