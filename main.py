from __future__ import annotations

import asyncio
import json
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
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
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
    PaymentVerifyRequest,
    Problem,
    PurchaseAuthorizeRequest,
    PurchaseReviewRequest,
    VendorCreate,
    VendorPatch,
)
from services.catalog_service import CatalogService
from services.normalization_service import NormalizationService
from services.payments import (
    PaymentUnavailable,
    RazorpayAgenticProvider,
    build_payment_provider,
)

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


def _sse(event: str, payload: dict[str, Any]) -> str:
    """Encode one compact server-sent event without permitting line injection."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def create_app(
    settings: Settings | None = None,
    mongo_client: MongoClient[dict[str, Any]] | None = None,
    payment_provider: Any | None = None,
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
    payments = (
        payment_provider if payment_provider is not None else build_payment_provider(resolved)
    )
    agentic = RazorpayAgenticProvider()

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
    app.state.payments = payments
    app.state.agentic = agentic
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

    @app.exception_handler(PaymentUnavailable)
    async def payment_unavailable(request: Request, error: PaymentUnavailable) -> JSONResponse:
        """Keep missing payment capabilities as safe client errors."""
        return _problem(request, 400, str(error))

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

    @app.get("/shop", include_in_schema=False)
    def shopper_portal(request: Request) -> RedirectResponse:
        """Give presenters and shoppers a memorable URL for the customer storefront."""
        return RedirectResponse(f"{request.url_for('dashboard')}#store")

    @app.get(f"{config.routes.api}/health", tags=["operations"])
    def health() -> dict[str, str]:
        """Report process health without claiming database readiness."""
        return {"status": "ok", "name": config.app.name, "version": config.app.version}

    @app.get(f"{config.routes.api}/ready", tags=["operations"])
    def ready() -> dict[str, str]:
        """Confirm Mongo is reachable for deployment readiness probes."""
        client.admin.command("ping")
        return {"status": "ready"}

    def _checkout_payload(purchase: dict[str, Any], vendor: dict[str, Any]) -> dict[str, Any]:
        """Add the public Razorpay Key ID only when a backend order already exists."""
        checkout = dict(purchase.get("checkout") or {})
        if not checkout.get("order_id") or not resolved.razorpay_key_id:
            purchase.pop("checkout", None)
            return purchase
        checkout["key_id"] = resolved.razorpay_key_id
        checkout["name"] = vendor.get("name") or config.app.name
        purchase["checkout"] = checkout
        return purchase

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

    @management.post("/vendors/{reference}/chat/stream")
    async def stream_chat(
        reference: str, payload: ChatRequest, request: Request
    ) -> StreamingResponse:
        """Stream safe agent activity before delivering the complete grounded answer."""
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

        async def events():
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            async def publish(event: dict[str, Any]) -> None:
                await queue.put(event)

            task = asyncio.create_task(
                request.app.state.agent_browser.run(
                    payload.message, entry, payload.history, on_event=publish
                )
            )
            yield _sse(
                "activity",
                {
                    "label": "Understanding your request",
                    "detail": f"Searching {vendor.get('name') or 'the selected store'}",
                    "operation": "start",
                },
            )
            try:
                while not task.done() or not queue.empty():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.15)
                    except TimeoutError:
                        continue
                    yield _sse("activity", event)
                result = await task
                yield _sse("result", result)
            except AgentResponseError:
                yield _sse("error", {"detail": config.model.unavailable})
            except Exception as error:
                LOGGER.exception("Streaming chat failed", exc_info=error)
                yield _sse("error", {"detail": "The grounded answer could not be completed."})
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @management.post("/vendors/{reference}/purchases/review")
    def review_purchase(reference: str, payload: PurchaseReviewRequest) -> dict[str, Any]:
        """Rebuild a purchase summary from live catalog data without charging or stocking writes."""
        _vendor_or_404(catalog, reference)
        return {"purchase": catalog.review_purchase(reference, payload.items)}

    @management.post("/vendors/{reference}/purchases/{attempt_id}/authorize")
    def authorize_purchase(
        reference: str, attempt_id: str, payload: PurchaseAuthorizeRequest
    ) -> dict[str, Any]:
        """Accept explicit confirmation, store a spending bound, then start Checkout."""
        vendor = _vendor_or_404(catalog, reference)
        purchase = catalog.authorize_purchase(
            reference, attempt_id, payload.confirm, payload.max_amount
        )
        message = (
            "Your purchase is authorized. Payment has not started yet, "
            "and inventory was not changed."
        )
        if resolved.razorpay_enabled:
            if not payments.available():
                raise PaymentUnavailable(
                    "Razorpay Test Mode is enabled but credentials are missing."
                )
            amount_minor, currency = catalog.payable_amount(reference, attempt_id)
            created = payments.create_payment(
                amount_minor=amount_minor,
                currency=currency,
                receipt=purchase["id"][:40],
                notes={"attempt": purchase["id"]},
            )
            created_order = {
                "provider_order_id": created.provider_order_id,
                "amount_minor": amount_minor,
                "currency": currency,
            }
            if int(created.amount_minor) != amount_minor or created.currency != currency:
                raise ValueError("The payment provider amount did not match the catalog total.")
            purchase = catalog.start_provider_payment(
                reference, attempt_id, created_order, payments.name
            )
            purchase = _checkout_payload(purchase, vendor)
            message = "Preparing secure payment..."
        return {
            "purchase": purchase,
            "message": message,
            "agentic": {"available": agentic.available()},
        }

    @management.post("/vendors/{reference}/purchases/{attempt_id}/payment/verify")
    def verify_payment(
        reference: str, attempt_id: str, payload: PaymentVerifyRequest
    ) -> dict[str, Any]:
        """Verify Checkout results server-side before any inventory change."""
        _vendor_or_404(catalog, reference)
        purchase = catalog.verify_and_fulfill_payment(
            reference,
            attempt_id,
            browser_order_id=payload.razorpay_order_id,
            payment_id=payload.razorpay_payment_id,
            signature=payload.razorpay_signature,
            provider=payments,
        )
        paid = purchase.get("status") == "paid"
        pending = (purchase.get("payment") or {}).get("status") == "verification_pending"
        if pending:
            return {
                "purchase": purchase,
                "retryable": True,
                "message": (
                    "Payment may have succeeded, but we couldn't confirm it with Razorpay yet. "
                    "Don't pay again. Retry payment confirmation."
                ),
            }
        return {
            "purchase": purchase,
            "message": (
                "Payment successful. Your order has been confirmed. Inventory has been updated."
                if paid
                else "Payment was not completed. No inventory was changed."
            ),
        }

    @management.post("/vendors/{reference}/purchases/{attempt_id}/payment/failed")
    def payment_failed(reference: str, attempt_id: str) -> dict[str, Any]:
        """Record an abandoned Checkout without fulfilling or decrementing stock."""
        _vendor_or_404(catalog, reference)
        return {
            "purchase": catalog.mark_payment_failed(reference, attempt_id),
            "message": "Payment was not completed. No inventory was changed.",
        }

    @management.post("/vendors/{reference}/purchases/{attempt_id}/cancel")
    def cancel_purchase(reference: str, attempt_id: str) -> dict[str, Any]:
        """Cancel a review or authorization without creating a paid order."""
        _vendor_or_404(catalog, reference)
        return {
            "purchase": catalog.cancel_purchase(reference, attempt_id),
            "message": "Purchase cancelled. Inventory was not changed.",
        }

    @app.post(f"{config.routes.api}/payments/razorpay/webhook")
    async def razorpay_webhook(request: Request) -> dict[str, str]:
        """Verify raw webhook bodies and funnel captured payments through one fulfillment path."""
        body = await request.body()
        signature = request.headers.get("X-Razorpay-Signature", "")
        event_id = request.headers.get("X-Razorpay-Event-Id") or ""
        try:
            payments.verify_webhook(body, signature)
            payload = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            LOGGER.warning("Razorpay webhook rejected (%s)", "signature")
            raise HTTPException(
                status_code=400, detail="The webhook could not be verified."
            ) from None
        event = str(payload.get("event") or "")
        entity = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
        if event == "order.paid":
            entity = ((payload.get("payload") or {}).get("order") or {}).get("entity") or entity
        order_id = str(entity.get("order_id") or entity.get("id") or "")
        payment_id = (
            str(entity.get("id") or "") if event != "order.paid" else str(entity.get("id") or "")
        )
        found = catalog.find_purchase_by_provider_order(order_id)
        if not found:
            return {"status": "ignored"}
        vendor, document = found
        already_paid = document.get("status") == "paid" or document.get("fulfillment") == "complete"
        if event_id and event_id in (document.get("webhook_ids") or []) and already_paid:
            return {"status": "ok"}
        if event in {"payment.failed"}:
            catalog.mark_payment_failed(vendor["_id"], document["attempt_id"])
            if event_id:
                catalog.purchases.update_one(
                    {"_id": document["_id"]}, {"$addToSet": {"webhook_ids": event_id}}
                )
            return {"status": "ok"}
        if event in {"payment.captured", "order.paid"}:
            live_payment = str(entity.get("id") or payment_id)
            if event == "order.paid":
                live_payment = str(
                    ((payload.get("payload") or {}).get("payment") or {})
                    .get("entity", {})
                    .get("id")
                    or live_payment
                )
            catalog.finalize_successful_payment(
                vendor,
                document,
                payment_id=live_payment,
                order_id=str((document.get("payment") or {}).get("razorpay_order_id") or order_id),
                amount_minor=int(entity.get("amount") or document["total_minor"]),
                currency=str(entity.get("currency") or document["currency"]),
                captured=True,
                live_order_id=str(entity.get("order_id") or order_id),
                live_status="captured",
            )
            if event_id:
                catalog.purchases.update_one(
                    {"_id": document["_id"]}, {"$addToSet": {"webhook_ids": event_id}}
                )
        return {"status": "ok"}

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
