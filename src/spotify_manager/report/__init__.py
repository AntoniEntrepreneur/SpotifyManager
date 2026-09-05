"""Rendering a plan as a document a human can review, and serving it for approval.

`render.py` is pure: a plan goes in, an HTML string comes out. Writing that string
to a file, archiving it and opening a browser are the caller's business, which is
what keeps re-rendering free of any request, file or clock. `server.py` is the other
half -- it puts that string on a loopback port and waits for a decision to come back
-- and it deliberately decides nothing itself.
"""

from __future__ import annotations

from .render import render_plan_html, render_results_html
from .server import (
    APPROVE_PATH,
    DEFAULT_PORT,
    RESULTS_PATH,
    ApprovalServer,
    PortUnavailableError,
)

__all__ = [
    "APPROVE_PATH",
    "DEFAULT_PORT",
    "ApprovalServer",
    "PortUnavailableError",
    "RESULTS_PATH",
    "render_plan_html",
    "render_results_html",
]
