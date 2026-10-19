"""Parsing-specific exceptions."""

from __future__ import annotations


class ParseError(RuntimeError):
    """A PDF cannot be parsed or produced no usable content."""


class MineruError(RuntimeError):
    """The MinerU cloud parser failed (caller decides whether to fall back)."""
