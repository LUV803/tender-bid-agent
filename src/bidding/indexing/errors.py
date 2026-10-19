"""Indexing-specific exceptions."""

from __future__ import annotations


class IngestError(RuntimeError):
    """Raised when chunking, embedding or Chroma storage fails."""
