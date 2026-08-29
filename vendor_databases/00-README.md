# Source Fixtures

This folder contains representative merchant-source data used for local development, demonstrations, and normalization tests.

- `1/` — a small CSV product catalog.
- `amazon/` — a larger Amazon-derived sample with JSON snapshots and a multi-table SQLite catalog.

These files are input fixtures. The application reads them and publishes normalized records to MongoDB; generated normalized output does not belong beside them.
