"""Multi-agent bid-writing pipeline.

Public entry point: :func:`write_bid`; CLI: ``python -m bidding write``.
"""

from .agents import BidAgents
from .errors import BidWriteError
from .graph import build_bid_graph, run_writing_graph
from .outline import BidOutline, BidSection, build_outline, build_response_matrix
from .pipeline import BidWriteResult, default_profile_path, write_bid
from .profile import BidderProfile, load_profile
from .render import (
    build_bid_markdown,
    build_review_markdown,
    collect_pending_markers,
    export_docx,
)
from .retrieval import Evidence, TenderRetriever
from .tender import TenderContext, load_tender_context

__all__ = [
    "BidAgents",
    "BidOutline",
    "BidSection",
    "BidWriteError",
    "BidWriteResult",
    "BidderProfile",
    "Evidence",
    "TenderContext",
    "TenderRetriever",
    "build_bid_graph",
    "build_bid_markdown",
    "build_outline",
    "build_response_matrix",
    "build_review_markdown",
    "collect_pending_markers",
    "default_profile_path",
    "export_docx",
    "load_profile",
    "load_tender_context",
    "run_writing_graph",
    "write_bid",
]
