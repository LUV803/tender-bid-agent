"""Extraction-specific exceptions."""

from __future__ import annotations


class ExtractError(RuntimeError):
    """An extraction failure.

    ``truncated`` lets the caller bisect the input; ``fatal`` means retrying
    other units cannot succeed either (bad key/model, rejected request).
    """

    def __init__(
        self, message: str, *, truncated: bool = False, fatal: bool = False
    ) -> None:
        super().__init__(message)
        self.truncated = truncated
        self.fatal = fatal
