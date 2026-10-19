"""Run the API with ``uv run python -m api``."""

from __future__ import annotations

import uvicorn

from bidding.config import get_settings

from .app import app


def main() -> None:
    settings = get_settings()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")


if __name__ == "__main__":
    main()
