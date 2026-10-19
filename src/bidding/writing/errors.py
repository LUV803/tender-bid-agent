"""Errors raised by the multi-agent bid-writing pipeline."""

from __future__ import annotations


class BidWriteError(RuntimeError):
    """Raised when a bid cannot be planned, written, reviewed or exported.

    ``fatal`` marks errors that make further LLM calls pointless (for example
    authentication failures) so the pipeline can stop instead of retrying every
    section.
    """

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal
