# Amazon Sample Fixtures

This folder contains the larger, multi-format source sample used to exercise lossless ingestion at a more realistic scale.

- `catalog.sqlite3` — the relational fixture with multiple tables and relationships.
- `products.json` — a structured product snapshot.
- `raw_products.json` — the raw product snapshot retained for source-fidelity checks.

These are source inputs, not application databases. Do not add generated normalized JSON files here.
