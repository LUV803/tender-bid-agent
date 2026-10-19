"""Bidding document multi-agent system."""

from __future__ import annotations

__version__ = "0.1.0"


def main() -> int:
    from .cli import main as cli_main

    return cli_main()
