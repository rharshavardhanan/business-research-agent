"""Vercel entry point.

Vercel serves the ASGI application exported from this module. All routing is
handled by FastAPI itself (see vercel.json), including the static UI, so there
is exactly one function and one place that decides what a URL means.
"""

from app.main import app

__all__ = ["app"]
