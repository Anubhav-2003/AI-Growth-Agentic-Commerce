from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import quote

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from agent_web import create_agent_app
from config import Settings, get_settings
from model_layer import AgentBrowser, AgentResponseError, ModelGateway
from models import (
    ChatRequest,
    MappingUpdate,
    Problem,
    PurchaseAuthorizeRequest,
    PurchaseReviewRequest,
    VendorCreate,
    VendorPatch,
)
from services.catalog_service import CatalogService
from services.normalization_service import NormalizationService

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent


def _problem(request: Request, status: int, detail: str) -> JSONResponse:
    """Return one safe RFC 9457-shaped management API failure."""
    body = Problem(
        type="about:blank",
        title=HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        instance=str(request.url),
    )
    return JSONResponse(
        body.model_dump(exclude_none=True),
        status_code=status,
        media_type="application/problem+json",
    )


def _vendor_or_404(catalog: CatalogService, reference: str) -> dict[str, Any]:
    """Resolve a management vendor reference without leaking Mongo coercion errors."""
    vendor = catalog.get_vendor(reference)
    if vendor is None:
        raise HTTPException(status_code=404, detail="The requested storefront was not found.")
    return vendor


def _sync_view(sync: dict[str, Any]) -> dict[str, Any]:
    """Expose convenient counts while retaining the complete revision ledger."""
    result, counts = dict(sync), dict(sync.get("counts") or {})
    result.update(
        resources=counts.get("resources", result.get("resources", 0)),
        records=counts.get("records", result.get("records", 0)),
        warning_count=counts.get("warnings", len(result.get("warnings") or [])),
    )
    return result


def create_app(
    settings: Settings | None = None,
    mongo_client: MongoClient[dict[str, Any]] | None = None,
) -> FastAPI:
    """Compose the outer control plane and nested agent website around shared services."""
    resolved, owns_client = settings or get_settings(), mongo_client is None
    config = resolved.commerce
    if (
        resolved.app_env.lower() not in config.security.local_environments
        and resolved.admin_api_key is None
    ):
        raise RuntimeError("ADMIN_API_KEY is required outside configured local environments.")
    client = mongo_client or MongoClient(
        resolved.mongodb_uri,
        serverSelectionTimeoutMS=config.limits.mongo_timeout_milliseconds,
    )
    catalog = CatalogService(client[resolved.mongodb_database], config)
    normalizer = NormalizationService(catalog, resolved.source_roots, config.formats, config.limits)
    model = ModelGateway(resolved, config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """Verify persistence and indexes before serving, then close owned connections."""
        try:
            client.admin.command("ping")
            catalog.ensure_indexes()
            yield
        finally:
            if owns_client:
                client.close()

    app = FastAPI(
        title=config.app.name,
        description=config.app.tagline,
        version=config.app.version,
        lifespan=lifespan,
    )
    app.state.catalog = catalog
    app.state.normalizer = normalizer
    app.state.model = model
    app.state.mongo_client = client
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_body_size=config.limits.max_request_bytes,
    )
    templates = Jinja2Templates(directory=PROJECT_ROOT / "human_ui")
    api_key = APIKeyHeader(name=config.security.admin_header, auto_error=False)

    async def require_operator(supplied: str | None = Security(api_key)) -> None:
        """Require the configured operator key while leaving local no-key mode explicit."""
        if resolved.admin_api_key is None:
            return
        expected = resolved.admin_api_key.get_secret_value()
        if supplied is None or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="A valid operator API key is required.")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _error: RequestValidationError) -> JSONResponse:
        """Keep invalid management inputs actionable without echoing unsafe values."""
        return _problem(request, 422, "The request does not match the endpoint input schema.")

    @app.exception_handler(DuplicateKeyError)
    async def duplicate_error(request: Request, _error: DuplicateKeyError) -> JSONResponse:
        """Translate Mongo uniqueness conflicts into a stable public response."""
        return _problem(request, 409, "A storefront with that identifier already exists.")

    @app.exception_handler(FileNotFoundError)
    async def missing_error(request: Request, error: FileNotFoundError) -> JSONResponse:
        """Expose missing configured resources without an internal traceback."""
        return _problem(request, 404, str(error))

    @app.exception_handler(PermissionError)
    async def permission_error(request: Request, _error: PermissionError) -> JSONResponse:
        """Hide approved root locations when a source attempts to escape them."""
        return _problem(request, 403, "The configured source path is not permitted.")

    @app.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError) -> JSONResponse:
        """Return deterministic source and cursor validation failures as bad requests."""
        return _problem(request, 400, str(error))

    @app.exception_handler(AgentResponseError)
    async def agent_response_error(request: Request, _error: AgentResponseError) -> JSONResponse:
        """Hide native provider failures and malformed decisions behind one safe boundary."""
        return _problem(request, 502, config.model.unavailable)

    @app.exception_handler(PyMongoError)
    async def mongo_error(request: Request, _error: PyMongoError) -> JSONResponse:
        """Report persistence unavailability without exposing server coordinates."""
        return _problem(request, 503, "Catalog storage is temporarily unavailable.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        """Normalize framework failures to the management problem contract."""
        detail = str(error.detail) if error.status_code < 500 else "The request could not complete."
        return _problem(request, error.status_code, detail)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        """Log unexpected failures while returning no implementation detail."""
        LOGGER.exception("Unhandled management API failure", exc_info=error)
        return _problem(request, 500, "The request could not complete.")

    @app.get("/", name="dashboard", include_in_schema=False)
    def dashboard(request: Request):
        """Render the control plane with route and protocol values from shared config."""
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "app_name": config.app.name,
                "api_root": config.routes.api,
                "agent_root": config.routes.agent,
                "static_root": config.routes.static,
                "vendor_key": config.app.browser_storage_key,
                "agent_page_version": config.agent_page.version,
                "ucp_version": config.ucp.version,
                "chat_question_limit": config.limits.chat_question_characters,
                "chat_history_limit": config.limits.chat_history_messages,
            },
        )

    @app.get("/register", include_in_schema=False)
    @app.get("/login", include_in_schema=False)
    @app.get("/home", include_in_schema=False)
    def legacy_dashboard(request: Request) -> RedirectResponse:
        """Preserve old bookmarks while replacing vendor-ID login with the real dashboard."""
        target = str(request.url_for("dashboard"))
        return RedirectResponse(f"{target}#sources" if request.url.path == "/register" else target)

    @app.get(f"{config.routes.api}/health", tags=["operations"])
    def health() -> dict[str, str]:
        """Report process health without claiming database readiness."""
        return {"status": "ok", "name": config.app.name, "version": config.app.version}

    @app.get(f"{config.routes.api}/ready", tags=["operations"])
    def ready() -> dict[str, str]:
        """Confirm Mongo is reachable for deployment readiness probes."""
        client.admin.command("ping")
        return {"status": "ready"}

    management = APIRouter(
        prefix=config.routes.api,
        tags=["management"],
        dependencies=[Depends(require_operator)],
    )

    @management.get("/vendors")
    def list_vendors() -> dict[str, Any]:
        """List operator-visible storefronts in a stable collection envelope."""
        return {"items": catalog.list_vendors()}

    @management.post("/vendors", status_code=201)
    def create_vendor(payload: VendorCreate) -> dict[str, Any]:
        """Register one source configuration without reading it before an explicit sync."""
        return {"vendor": catalog.create_vendor(payload)}

    @management.get("/vendors/{reference}")
    def get_vendor(reference: str) -> dict[str, Any]:
        """Return current storefront configuration alongside published totals."""
        vendor = _vendor_or_404(catalog, reference)
        return {"vendor": vendor, "stats": catalog.stats(reference)}

    @management.patch("/vendors/{reference}")
    def update_vendor(reference: str, payload: VendorPatch) -> dict[str, Any]:
        """Apply only validated editable fields while retaining unknown legacy metadata."""
        _vendor_or_404(catalog, reference)
        return {"vendor": catalog.update_vendor(reference, payload)}

    @management.post("/vendors/{reference}/sync")
    def sync_vendor(reference: str) -> dict[str, Any]:
        """Publish one atomic lossless revision from the storefront's configured source."""
        _vendor_or_404(catalog, reference)
        summary = _sync_view(normalizer.run(reference))
        return {"message": "Catalog synchronization completed.", "sync": summary}

    @management.get("/vendors/{reference}/syncs")
    def list_syncs(reference: str) -> dict[str, Any]:
        """Return the bounded publication ledger with complete warning details."""
        _vendor_or_404(catalog, reference)
        return {"items": [_sync_view(item) for item in catalog.list_syncs(reference)]}

    @management.get("/vendors/{reference}/resources")
    def list_resources(reference: str) -> dict[str, Any]:
        """List every resource in the active lossless revision."""
        _vendor_or_404(catalog, reference)
        return {"items": catalog.list_resources(reference)}

    @management.get("/vendors/{reference}/records")
    def list_records(
        reference: str,
        resource: str | None = None,
        cursor: str | None = None,
        limit: int | None = Query(default=None, ge=1, le=config.limits.max_page_size),
        q: str | None = Query(default=None, max_length=config.limits.max_query_length),
    ) -> dict[str, Any]:
        """Browse or search a bounded active-revision record page."""
        _vendor_or_404(catalog, reference)
        return catalog.list_records(reference, resource, cursor=cursor, limit=limit, query=q)

    @management.put("/vendors/{reference}/mapping")
    def update_mapping(reference: str, payload: MappingUpdate) -> dict[str, Any]:
        """Publish an explicit projection mapping without changing normalized truth."""
        _vendor_or_404(catalog, reference)
        if catalog.get_resource(reference, payload.mapping.resource) is None:
            raise HTTPException(status_code=404, detail="The mapped resource was not found.")
        return {"vendor": catalog.update_mapping(reference, payload)}

    @management.post("/vendors/{reference}/chat")
    async def chat(reference: str, payload: ChatRequest, request: Request) -> dict[str, Any]:
        """Browse the live machine storefront and return its grounded answer and trace."""
        vendor = _vendor_or_404(catalog, reference)
        if len(payload.message) > config.limits.chat_question_characters:
            raise HTTPException(status_code=400, detail="The chat question is too long.")
        if len(payload.history) > config.limits.chat_history_messages:
            raise HTTPException(status_code=400, detail="The chat history is too long.")
        if sum(len(item.get("content", "")) for item in payload.history) > (
            config.limits.chat_context_characters
        ):
            raise HTTPException(status_code=400, detail="The chat history is too large.")
        root = str(request.base_url).rstrip("/")
        entry = f"{root}{config.routes.agent}/{quote(str(vendor['slug']), safe='')}/"
        return await request.app.state.agent_browser.run(payload.message, entry, payload.history)

    @management.post("/vendors/{reference}/purchases/review")
    def review_purchase(reference: str, payload: PurchaseReviewRequest) -> dict[str, Any]:
        """Rebuild a purchase summary from live catalog data without charging or stocking writes."""
        _vendor_or_404(catalog, reference)
        return {"purchase": catalog.review_purchase(reference, payload.items)}

    @management.post("/vendors/{reference}/purchases/{attempt_id}/authorize")
    def authorize_purchase(
        reference: str, attempt_id: str, payload: PurchaseAuthorizeRequest
    ) -> dict[str, Any]:
        """Accept only explicit confirmation, then stop before payment-provider execution."""
        _vendor_or_404(catalog, reference)
        return {
            "purchase": catalog.authorize_purchase(reference, attempt_id, payload.confirm),
            "message": (
                "Your purchase is authorized. Payment has not started yet, "
                "and inventory was not changed."
            ),
        }

    @management.post("/vendors/{reference}/purchases/{attempt_id}/cancel")
    def cancel_purchase(reference: str, attempt_id: str) -> dict[str, Any]:
        """Cancel a review or authorization without creating a paid order."""
        _vendor_or_404(catalog, reference)
        return {
            "purchase": catalog.cancel_purchase(reference, attempt_id),
            "message": "Purchase cancelled. Inventory was not changed.",
        }

    app.include_router(management)
    app.mount(
        config.routes.static,
        StaticFiles(directory=PROJECT_ROOT / "human_ui"),
        name="static",
    )
    app.mount(config.routes.agent, create_agent_app(resolved, catalog), name="agent")
    app.state.agent_browser = AgentBrowser(model, app, config)
    return app


app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_env.lower() in settings.commerce.security.local_environments,
    )
