# Model Layer

This folder contains the optional, thin model-provider integration used by grounded catalog chat.

- `client.py` — AnyLLM gateway, bounded context handling, provider configuration, and response extraction.
- `__init__.py` — package exports.

This layer is not involved in source ingestion, normalization, schema discovery, record identity, or commerce mapping.
