# Application Services

This folder contains the core service boundaries behind the management API and agent web layer.

- `normalization_service.py` — deterministic CSV, JSON, and SQLite inventory, extraction, lossless normalization, revision staging, and mapping suggestions.
- `catalog_service.py` — MongoDB persistence, indexes, revision queries, search, pagination, and additive commerce projections.

Services operate on the shared models and configuration. They are the only application layer that owns source ingestion and catalog persistence.
