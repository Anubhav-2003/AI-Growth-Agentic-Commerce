# Agent Web Layer

This folder contains the nested FastAPI application that exposes CommerceOS as a machine-first website for AI agents.

- `app.py` — linked JSON page routes, page builders, schemas, search, and UCP catalog responses.
- `__init__.py` — package exports used by the outer application.

The layer reads published catalog data through the catalog service. It does not serve the human dashboard and does not parse source files.
