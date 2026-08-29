# Repository Map

CommerceOS is organized around the four runtime boundaries described in the implementation plan: the outer FastAPI application, deterministic normalization and catalog services, the agent-facing web layer, and the optional model layer.

## Where things live

- `docs/` — product vision, architecture/implementation plan, current status, and the original idea note.
- `main.py`, `config.py`, and `models.py` — outer application entry point, centralized settings, and shared Pydantic contracts.
- `services/` — deterministic source normalization and MongoDB catalog persistence.
- `agent_web/` — linked JSON pages and UCP catalog endpoints for AI agents.
- `model_layer/` — thin AnyLLM gateway and model-backed chat integration.
- `human_ui/` — the browser dashboard's HTML, CSS, and JavaScript assets.
- `tests/` — automated tests for the agent web layer, services, model layer, and management API.
- `vendor_databases/` — source fixtures used for local development and normalization tests.
- `config/` — non-secret YAML configuration shared by the application.

The `00-README.md` file at the top of each maintained folder explains that folder's scope. `README.md` remains the starting point for installation and usage. `.env` and `.venv/` are local-only development assets; generated caches and normalized output files do not belong in the repository.
