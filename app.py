"""Vercel-recognized FastAPI entrypoint.

The application itself lives in ``main.py`` so local development can continue to
use ``uvicorn main:app``. Vercel auto-detects a root-level ``app.py`` exporting
``app`` and serves the full ASGI application from it.
"""

from main import app

__all__ = ["app"]
