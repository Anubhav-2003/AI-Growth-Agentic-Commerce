# Application Services

This folder contains the core service boundaries behind the management API and agent web layer.

- `catalog_service.py` — MongoDB persistence, indexes, revision queries, search, pagination, additive commerce projections, purchase attempts, and idempotent fulfillment.
- `normalization_service.py` — deterministic CSV, JSON, and SQLite inventory, extraction, lossless normalization, revision staging, and mapping suggestions.
- `payments.py` — Razorpay Standard Checkout TEST adapter plus an unavailable Agentic Payments adapter.

Services operate on the shared models and configuration. They are the only application layer that owns source ingestion and catalog persistence.
