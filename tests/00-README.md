# Test Suite

This folder contains pytest coverage for the implemented CommerceOS boundaries and workflows.

- `test_services.py` — normalization, catalog persistence, revision, and source-fidelity tests.
- `test_agent_web.py` — linked JSON page traversal and UCP endpoint tests.
- `test_model_layer.py` — AnyLLM gateway and deterministic fallback tests.
- `test_management_api.py` — outer FastAPI management, dashboard, health, and synchronization tests.

Tests may use the local MongoDB configured for development; they do not replace source fixtures or production data.
