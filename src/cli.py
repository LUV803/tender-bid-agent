"""Compatibility entry point for ``python -m src.cli``.

The installed console entry point is ``python -m bidding``; this module makes
the command requested in the project brief work from the repository root
without changing the ``src`` layout.
"""

from __future__ import annotations

from bidding.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
