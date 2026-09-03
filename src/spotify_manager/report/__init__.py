"""Rendering a plan as a document a human can review.

`render.py` is pure: a plan goes in, an HTML string comes out. Writing that string
to a file, archiving it and opening a browser are the caller's business, which is
what keeps re-rendering free of any request, file or clock.
"""

from __future__ import annotations

from .render import render_plan_html

__all__ = ["render_plan_html"]
