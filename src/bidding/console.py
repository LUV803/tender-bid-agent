"""Console encoding helpers for Windows pipes and terminals."""

from __future__ import annotations

import sys


def configure_stdio() -> None:
    """Emit UTF-8 when stdout/stderr are captured by a pipe.

    Interactive Windows terminals keep their native encoding so Chinese
    output continues to match the console code page.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
