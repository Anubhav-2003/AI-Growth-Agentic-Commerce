"""Linked JSON and UCP storefronts for machine clients."""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Mapping
from copy import deepcopy
from http import HTTPStatus
from typing import Any, Protocol, TypeVar

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import URL
from ucp_sdk.models.schemas.common.types.error_response import (
    ErrorResponse as UcpErrorResponse,
)
from ucp_sdk.models.schemas.profile import BusinessSchema as UcpBusinessProfile
from ucp_sdk.models.schemas.shopping.catalog_lookup import (
    GetProductRequest,
    GetProductResponse,
    LookupRequest,
    LookupResponse,
)
from ucp_sdk.models.schemas.shopping.catalog_search import SearchRequest, SearchResponse

from config import CommerceConfig, Settings
from models import Action, AgentPage, Link, PageIdentity, Problem

LOGGER = logging.getLogger(__name__)
DEFAULT_VARIANT_SUFFIX = "::default"
RESOURCE_TOKEN_PREFIX = "~"
SAFE_RESOURCE_SEGMENT = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
ModelT = TypeVar("ModelT", bound=BaseModel)


class Catalog(Protocol):
    """Describe the shallow catalog-service boundary consumed by this site."""

    def get_vendor_by_slug(self, slug: str, public_only: bool = False) -> dict[str, Any] | None:
        """Resolve a storefront by its public machine-site slug."""
        ...

    def list_resources(self, vendor_id: str, sync_id: str | None = None) -> list[dict[str, Any]]:
        """List normalized resources from one published revision."""
        ...

    def get_resource(
        self, vendor_id: str, name: str, sync_id: str | None = None
    ) -> dict[str, Any] | None:
        """Resolve one named normalized resource descriptor."""
        ...

    def list_records(
        self,
        vendor_id: str,
        resource: str | None = None,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        query: str | None = None,
        commerce_only: bool = False,
    ) -> dict[str, Any]:
        """Page lossless records through the shared catalog contract."""
        ...

    def get_record(self, vendor_id: str, record_id: str) -> dict[str, Any] | None:
        """Resolve one active record by stable or mapped identity."""
        ...

    def search_records(
        self,
        vendor_id: str,
        query: str,
        *,
        resource: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        commerce_only: bool = False,
    ) -> dict[str, Any]:
        """Search a bounded active catalog page."""
        ...

    def project_record(
        self, record: Mapping[str, Any], mapping: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Apply only the explicit merchant mapping to a raw record."""
        ...


class SearchInput(BaseModel):
    """Generate the generic search action's JSON input schema."""

    q: str = Field(min_length=1)
    resource: str | None = None
    cursor: str | None = None
    limit: int | None = Field(default=None, ge=1)


class AgentProblem(Exception):
    """Carry a deliberately safe problem from application code to HTTP."""

    def __init__(self, status: int, detail: str) -> None:
        """Retain only a status and client-safe explanation."""
        super().__init__(detail)
        self.status = status
        self.detail = detail


def encode_resource_name(value: str) -> str:
    """Keep ordinary names readable and encode names unsafe in one URL segment."""
    if SAFE_RESOURCE_SEGMENT.fullmatch(value) and not value.startswith(RESOURCE_TOKEN_PREFIX):
        return value
    payload = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
    return f"{RESOURCE_TOKEN_PREFIX}{payload}"


def _decode_resource_name(value: str) -> str:
    """Decode only canonical resource tokens emitted by this storefront."""
    if not value.startswith(RESOURCE_TOKEN_PREFIX):
        return value
    try:
        decoded = base64.urlsafe_b64decode(value[1:] + "=" * (-len(value[1:]) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as error:
        raise AgentProblem(400, "The resource identifier is invalid.") from error
    if encode_resource_name(decoded) != value:
        raise AgentProblem(400, "The resource identifier is invalid.")
    return decoded


def _config(settings: Settings | CommerceConfig) -> CommerceConfig:
    """Accept either complete settings or the already-loaded shared config."""
    return settings.commerce if isinstance(settings, Settings) else settings


def _href(app: FastAPI, request: Request, route: str, **params: str) -> str:
    """Reverse a child route and retain the mount root, scheme, and authority."""
    path = str(app.url_path_for(route, **params))
    root = str(request.scope.get("root_path", "")).rstrip("/")
    return str(request.url.replace(path=f"{root}{path}", query=None))


def _query(href: str, **values: Any) -> str:
    """Add only present query parameters through Starlette's URL encoder."""
    return str(
        URL(href).include_query_params(
            **{key: value for key, value in values.items() if value is not None}
        )
    )


def _cursor(scope: str, value: str | int | None, history: list[str | int | None]) -> str:
    """Encode service state and backwards history as a client-opaque token."""
    payload = json.dumps([scope, value, history], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _cursor_state(token: str | None, scope: str) -> tuple[str | int | None, list[str | int | None]]:
    """Decode only tokens issued for the current collection or search."""
    if token is None:
        return None, []
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        found_scope, value, history = json.loads(decoded)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise AgentProblem(400, "The pagination cursor is invalid.") from error
    if (
        found_scope != scope
        or not isinstance(history, list)
        or not isinstance(value, (str, int, type(None)))
    ):
        raise AgentProblem(400, "The pagination cursor does not belong to this page.")
    if not all(isinstance(item, (str, int, type(None))) for item in history):
        raise AgentProblem(400, "The pagination cursor is invalid.")
    return value, history


def _limit(config: CommerceConfig, value: int | None) -> int:
    """Apply the configured default and reject oversized requests explicitly."""
    limit = value or config.limits.default_page_size
    if limit > config.limits.max_page_size:
        raise AgentProblem(400, f"Page size cannot exceed {config.limits.max_page_size}.")
    return limit


def _vendor(catalog: Catalog, slug: str) -> dict[str, Any]:
    """Resolve only public stores without revealing whether private stores exist."""
    vendor = catalog.get_vendor_by_slug(slug, public_only=True)
    if vendor is None:
        raise AgentProblem(404, "The requested store was not found.")
    return vendor


def _vendor_id(vendor: Mapping[str, Any]) -> str:
    """Extract the serialized persistence identity required by catalog queries."""
    identifier = vendor.get("_id", vendor.get("id"))
    if identifier is None:
        raise AgentProblem(500, "The store is temporarily unavailable.")
    return str(identifier)


def _mapping(value: Any) -> dict[str, Any]:
    """Normalize stored Pydantic or mapping values without guessing fields."""
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    return dict(value) if isinstance(value, Mapping) else {}


def _ucp_ready(vendor: Mapping[str, Any]) -> bool:
    """Advertise catalog semantics only when every required fact is mapped."""
    mapping = _mapping(vendor.get("mapping"))
    fields = _mapping(mapping.get("fields"))
    facts = {"id", "title", "description", "price"}
    currency = "currency" in fields or bool(mapping.get("default_currency"))
    return bool(
        mapping.get("resource")
        and facts <= fields.keys()
        and mapping.get("price_units")
        and currency
    )


def _public_resource(resource: Mapping[str, Any]) -> dict[str, Any]:
    """Remove persistence ownership while retaining schema and revision facts."""
    return {key: value for key, value in resource.items() if key not in {"vendor_id"}}


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the lossless envelope while withholding internal search material."""
    return {key: value for key, value in record.items() if key not in {"vendor_id", "search_text"}}


def _record_entity(
    app: FastAPI, request: Request, store: str, record: Mapping[str, Any]
) -> dict[str, Any]:
    """Embed a bounded-page record with an absolute detail-page transition."""
    resource, identifier = (
        str(record.get("resource", "")),
        str(record.get("_id", record.get("id", ""))),
    )
    return {
        "id": identifier,
        "type": "record",
        "resource": resource,
        "href": _href(
            app,
            request,
            "record_page",
            store=store,
            resource=encode_resource_name(resource),
            record_id=identifier,
        ),
        "data": record.get("data", {}),
        "commerce": record.get("commerce"),
    }


def _meta(
    config: CommerceConfig,
    vendor: Mapping[str, Any],
    pagination: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach representation, language, revision, and optional page state."""
    result: dict[str, Any] = {
        "representation_version": str(config.agent_page.version),
        "language": config.app.language,
        "revision": vendor.get("active_sync_id", vendor.get("updated_at")),
    }
    if pagination is not None:
        result["pagination"] = dict(pagination)
    return result


def _agent_page(
    config: CommerceConfig,
    identity: PageIdentity,
    data: Any,
    links: list[Link],
    *,
    entities: list[Any] | None = None,
    actions: list[Action] | None = None,
    vendor: Mapping[str, Any],
    pagination: Mapping[str, Any] | None = None,
) -> AgentPage:
    """Validate the canonical linked representation before serialization."""
    return AgentPage(
        page=identity,
        data=data,
        entities=entities or [],
        links=links,
        actions=actions or [],
        meta=_meta(config, vendor, pagination),
    )


def _page_response(
    app: FastAPI, config: CommerceConfig, request: Request, page: AgentPage
) -> JSONResponse:
    """Serve profiled JSON with an HTTP describedby link to its schema."""
    schema = _href(app, request, "page_schema")
    headers = {"Link": f'<{schema}>; rel="describedby"; type="application/schema+json"'}
    media_type = f'{config.agent_page.content_type}; profile="{schema}"'
    return JSONResponse(
        page.model_dump(mode="json", exclude_none=True), media_type=media_type, headers=headers
    )


def _problem_response(
    request: Request, status: int, detail: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """Serialize a safe RFC 9457 problem without internal exception data."""
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "Request Failed"
    problem = Problem(
        type="about:blank", title=title, status=status, detail=detail, instance=str(request.url)
    )
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=status,
        media_type="application/problem+json",
        headers=dict(headers or {}),
    )


def _validated(model: type[ModelT], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and serialize every UCP document through the official SDK."""
    try:
        value = model.model_validate(payload)
    except ValidationError as error:
        raise AgentProblem(
            409, "The catalog projection does not satisfy the advertised UCP contract."
        ) from error
    return value.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)


def _ucp_meta(config: CommerceConfig, capability: str, *, error: bool = False) -> dict[str, Any]:
    """Build response metadata naming exactly one negotiated capability."""
    result: dict[str, Any] = {
        "version": config.ucp.version,
        "capabilities": {capability: [{"version": config.ucp.version}]},
    }
    if error:
        result["status"] = "error"
    return result


def _description(value: Any) -> dict[str, str]:
    """Retain an explicitly mapped description in its declared UCP format."""
    if isinstance(value, str):
        return {"plain": value}
    if isinstance(value, Mapping):
        formats = {
            key: value[key]
            for key in ("plain", "markdown", "html")
            if isinstance(value.get(key), str)
        }
        if formats:
            return formats
    raise AgentProblem(409, "A projected product is missing an explicit description.")


def _price(projected: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only an explicit minor-unit amount and ISO currency pair."""
    value = projected.get("price")
    if isinstance(value, Mapping):
        amount, currency = value.get("amount"), value.get("currency")
    else:
        amount, currency = value, projected.get("currency")
    if isinstance(amount, bool) or not isinstance(amount, int) or not isinstance(currency, str):
        raise AgentProblem(
            409, "A projected product is missing a valid minor-unit price and currency."
        )
    return {"amount": amount, "currency": currency.upper()}


def _product(projected: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt a deterministic commerce projection into an SDK-ready UCP product."""
    if projected.get("price_range") and projected.get("variants"):
        product = deepcopy(dict(projected))
        product["description"] = _description(product.get("description"))
        for variant in product["variants"]:
            variant["description"] = _description(variant.get("description"))
        return product

    identifier, title = projected.get("id"), projected.get("title")
    if not isinstance(identifier, str) or not isinstance(title, str):
        raise AgentProblem(409, "A projected product is missing an explicit ID or title.")
    description, price = _description(projected.get("description")), _price(projected)
    variant: dict[str, Any] = {
        "id": f"{identifier}{DEFAULT_VARIANT_SUFFIX}",
        "title": title,
        "description": description,
        "price": price,
    }
    availability = projected.get("availability")
    if isinstance(availability, bool):
        variant["availability"] = {"available": availability}
    elif isinstance(availability, str):
        variant["availability"] = {"status": availability}
    elif isinstance(availability, Mapping):
        variant["availability"] = dict(availability)
    product = {
        "id": identifier,
        "title": title,
        "description": description,
        "price_range": {"min": price, "max": price},
        "variants": [variant],
    }
    for key in ("handle", "url", "tags"):
        if projected.get(key) is not None:
            product[key] = projected[key]
    categories = projected.get("categories", projected.get("category"))
    if categories:
        values = categories if isinstance(categories, list) else [categories]
        product["categories"] = [
            item if isinstance(item, Mapping) else {"value": str(item), "taxonomy": "merchant"}
            for item in values
        ]
    media = projected.get("media", projected.get("image"))
    if media:
        values = media if isinstance(media, list) else [media]
        product["media"] = [
            item if isinstance(item, Mapping) else {"type": "image", "url": str(item)}
            for item in values
        ]
    if isinstance(projected.get("rating"), Mapping):
        product["rating"] = dict(projected["rating"])
    metadata = _mapping(projected.get("metadata"))
    for key in ("brand", "inventory"):
        if projected.get(key) is not None:
            metadata[key] = projected[key]
    if metadata:
        product["metadata"] = metadata
    return product


def _product_ids(product: Mapping[str, Any]) -> set[str]:
    """Collect exact product and variant identifiers accepted by lookup."""
    identifiers = {str(product.get("id", "")), str(product.get("handle", ""))}
    for variant in product.get("variants", []):
        identifiers.update(str(variant.get(key, "")) for key in ("id", "sku", "handle"))
    return identifiers - {""}


def _project(
    catalog: Catalog, vendor: Mapping[str, Any], record: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Apply only the merchant-approved deterministic mapping to one record."""
    projected = catalog.project_record(record, _mapping(vendor.get("mapping")))
    return _product(projected) if projected else None


def _find_product(
    catalog: Catalog,
    config: CommerceConfig,
    vendor: Mapping[str, Any],
    identifier: str,
) -> dict[str, Any] | None:
    """Resolve an exact UCP identifier without mistaking fuzzy search for lookup."""
    vendor_id = _vendor_id(vendor)
    direct = catalog.get_record(vendor_id, identifier)
    if direct:
        product = _project(catalog, vendor, direct)
        if product and identifier in _product_ids(product):
            return product
    query = identifier.removesuffix(DEFAULT_VARIANT_SUFFIX)
    page = catalog.search_records(
        vendor_id,
        query,
        resource=_mapping(vendor.get("mapping")).get("resource"),
        limit=config.limits.max_page_size,
        commerce_only=True,
    )
    for record in page.get("items", []):
        product = _project(catalog, vendor, record)
        if product and identifier in _product_ids(product):
            return product
    return None


def _lookup_product(product: Mapping[str, Any], identifiers: list[str]) -> dict[str, Any]:
    """Attach required request correlation only to resolving variants."""
    result = deepcopy(dict(product))
    product_ids = {str(result.get("id", "")), str(result.get("handle", ""))}
    variants = []
    for index, variant in enumerate(result.get("variants", [])):
        exact = [item for item in identifiers if item in _product_ids({"variants": [variant]})]
        featured = [item for item in identifiers if item in product_ids] if index == 0 else []
        correlations = [{"id": item, "match": "exact"} for item in exact]
        correlations.extend({"id": item, "match": "featured"} for item in featured)
        if correlations:
            variants.append({**variant, "inputs": correlations})
    result["variants"] = variants
    return result


def _ucp_error(config: CommerceConfig, identifier: str, detail: str) -> dict[str, Any]:
    """Return a validated HTTP-200 UCP business outcome."""
    payload = {
        "ucp": _ucp_meta(config, config.ucp.lookup_capability, error=True),
        "messages": [
            {
                "type": "error",
                "code": "not_found",
                "content": f"{detail}: {identifier}",
                "severity": "unrecoverable",
            }
        ],
    }
    return _validated(UcpErrorResponse, payload)


def create_agent_app(settings: Settings | CommerceConfig, catalog: Catalog) -> FastAPI:
    """Create the independently mountable machine storefront application."""
    config = _config(settings)
    app = FastAPI(
        title=f"{config.app.name} Agent Web",
        version=str(config.agent_page.version),
        docs_url=None,
        redoc_url=None,
    )
    profile_path = config.agent_page.profile_path
    mount_path = config.routes.agent.rstrip("/")
    inner_profile_path = (
        profile_path.removeprefix(mount_path)
        if profile_path.startswith(mount_path)
        else profile_path
    )

    @app.exception_handler(AgentProblem)
    async def agent_problem(request: Request, error: AgentProblem) -> JSONResponse:
        """Translate deliberate application failures into safe problem details."""
        return _problem_response(request, error.status, error.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_problem(request: Request, _error: RequestValidationError) -> JSONResponse:
        """Hide parser internals while identifying invalid request input."""
        return _problem_response(
            request, 422, "The request does not match this endpoint's input schema."
        )

    @app.exception_handler(HTTPException)
    async def http_problem(request: Request, error: HTTPException) -> JSONResponse:
        """Normalize framework routing failures to the same problem media type."""
        detail = (
            str(error.detail)
            if error.status_code < 500
            else "The store is temporarily unavailable."
        )
        return _problem_response(request, error.status_code, detail, error.headers)

    @app.exception_handler(Exception)
    async def unexpected_problem(request: Request, error: Exception) -> JSONResponse:
        """Log unexpected failures while returning no implementation detail."""
        LOGGER.exception("Unhandled agent storefront failure", exc_info=error)
        return _problem_response(request, 500, "The store is temporarily unavailable.")

    @app.get(inner_profile_path, name="page_schema", include_in_schema=False)
    def page_schema(request: Request) -> JSONResponse:
        """Publish the generated JSON Schema referenced by every generic page."""
        schema = AgentPage.model_json_schema()
        schema.update(
            {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": str(request.url)}
        )
        return JSONResponse(schema, media_type="application/schema+json")

    @app.get("/{store}/", name="store_home")
    def store_home(request: Request, store: str) -> JSONResponse:
        """Introduce a store through links that require no route knowledge."""
        vendor = _vendor(catalog, store)
        vendor_id = _vendor_id(vendor)
        resources = catalog.list_resources(vendor_id)
        self_url = _href(app, request, "store_home", store=store)
        entities = [
            {
                "id": str(item.get("name", "")),
                "type": "resource",
                "title": str(item.get("name", "")),
                "href": _href(
                    app,
                    request,
                    "resource_page",
                    store=store,
                    resource=encode_resource_name(str(item.get("name", ""))),
                ),
                "record_count": item.get("record_count"),
            }
            for item in resources[: config.limits.default_page_size]
        ]
        data = {
            "store": {
                "id": vendor_id,
                "name": vendor.get("name"),
                "slug": vendor.get("slug"),
                "status": vendor.get("status"),
            },
            "resource_count": len(resources),
            "ucp_catalog": "ready" if _ucp_ready(vendor) else "needs_mapping",
        }
        links = [
            Link(rel=["self"], href=self_url),
            Link(
                rel=["collection"],
                href=_href(app, request, "resources_page", store=store),
                title="Resources",
            ),
            Link(
                rel=["search"], href=_href(app, request, "search_page", store=store), title="Search"
            ),
            Link(
                rel=["schema"],
                href=_href(app, request, "store_schema", store=store),
                type=config.agent_page.content_type,
            ),
            Link(
                rel=["alternate"],
                href=_href(app, request, "ucp_profile", store=store),
                title="UCP discovery",
            ),
        ]
        action = Action(
            id="search",
            title="Search this store",
            method="GET",
            href=_href(app, request, "search_page", store=store),
            content_type=config.agent_page.content_type,
            input_schema=SearchInput.model_json_schema(),
        )
        page = _agent_page(
            config,
            PageIdentity(
                id=self_url,
                type="store",
                title=str(vendor.get("name", store)),
                summary=config.app.tagline,
            ),
            data,
            links,
            entities=entities,
            actions=[action],
            vendor=vendor,
        )
        return _page_response(app, config, request, page)

    @app.get("/{store}/resources", name="resources_page")
    def resources_page(
        request: Request,
        store: str,
        cursor: str | None = None,
        limit: int | None = Query(default=None, ge=1),
    ) -> JSONResponse:
        """List normalized resources with opaque forward and backward cursors."""
        vendor, page_limit = _vendor(catalog, store), _limit(config, limit)
        resources = catalog.list_resources(_vendor_id(vendor))
        scope = f"resources:{_vendor_id(vendor)}:{vendor.get('active_sync_id')}"
        value, history = _cursor_state(cursor, scope)
        offset = value if isinstance(value, int) else 0
        if offset < 0 or offset > len(resources):
            raise AgentProblem(400, "The pagination cursor is invalid.")
        selected, next_offset = resources[offset : offset + page_limit], offset + page_limit
        self_url = str(request.url)
        links = [
            Link(rel=["self"], href=self_url),
            Link(rel=["home"], href=_href(app, request, "store_home", store=store)),
        ]
        if next_offset < len(resources):
            links.append(
                Link(
                    rel=["next"],
                    href=_query(
                        _href(app, request, "resources_page", store=store),
                        cursor=_cursor(scope, next_offset, [*history, offset]),
                        limit=page_limit,
                    ),
                )
            )
        if history:
            links.append(
                Link(
                    rel=["prev"],
                    href=_query(
                        _href(app, request, "resources_page", store=store),
                        cursor=_cursor(scope, history[-1], history[:-1]),
                        limit=page_limit,
                    ),
                )
            )
        entities = [
            {
                **_public_resource(item),
                "href": _href(
                    app,
                    request,
                    "resource_page",
                    store=store,
                    resource=encode_resource_name(str(item.get("name", ""))),
                ),
            }
            for item in selected
        ]
        page = _agent_page(
            config,
            PageIdentity(
                id=self_url,
                type="resource-collection",
                title=f"{vendor.get('name', store)} resources",
            ),
            {"total": len(resources)},
            links,
            entities=entities,
            vendor=vendor,
            pagination={
                "limit": page_limit,
                "returned": len(selected),
                "total": len(resources),
                "has_next_page": next_offset < len(resources),
            },
        )
        return _page_response(app, config, request, page)

    @app.get("/{store}/resources/{resource}/schema", name="resource_schema")
    def resource_schema(request: Request, store: str, resource: str) -> JSONResponse:
        """Describe one source resource without assigning commerce semantics."""
        vendor = _vendor(catalog, store)
        resource_name = _decode_resource_name(resource)
        descriptor = catalog.get_resource(_vendor_id(vendor), resource_name)
        if descriptor is None:
            raise AgentProblem(404, "The requested resource was not found.")
        self_url = _href(app, request, "resource_schema", store=store, resource=resource)
        links = [
            Link(rel=["self"], href=self_url),
            Link(
                rel=["collection"],
                href=_href(app, request, "resource_page", store=store, resource=resource),
            ),
            Link(rel=["home"], href=_href(app, request, "store_home", store=store)),
        ]
        page = _agent_page(
            config,
            PageIdentity(id=self_url, type="resource-schema", title=f"{resource_name} schema"),
            descriptor.get("schema", {}),
            links,
            vendor=vendor,
        )
        return _page_response(app, config, request, page)

    @app.get("/{store}/resources/{resource}", name="resource_page")
    def resource_page(
        request: Request,
        store: str,
        resource: str,
        cursor: str | None = None,
        limit: int | None = Query(default=None, ge=1),
    ) -> JSONResponse:
        """Browse one resource as a collection of linked lossless records."""
        vendor, page_limit = _vendor(catalog, store), _limit(config, limit)
        vendor_id = _vendor_id(vendor)
        resource_name = _decode_resource_name(resource)
        descriptor = catalog.get_resource(vendor_id, resource_name)
        if descriptor is None:
            raise AgentProblem(404, "The requested resource was not found.")
        scope = f"records:{vendor_id}:{vendor.get('active_sync_id')}:{resource_name}"
        service_cursor, history = _cursor_state(cursor, scope)
        result = catalog.list_records(
            vendor_id,
            resource=resource_name,
            cursor=str(service_cursor) if service_cursor is not None else None,
            limit=page_limit,
        )
        next_value = result.get("next_cursor")
        self_url = str(request.url)
        links = [
            Link(rel=["self"], href=self_url),
            Link(rel=["collection"], href=_href(app, request, "resources_page", store=store)),
            Link(rel=["home"], href=_href(app, request, "store_home", store=store)),
            Link(
                rel=["schema"],
                href=_href(app, request, "resource_schema", store=store, resource=resource),
            ),
        ]
        if next_value is not None:
            links.append(
                Link(
                    rel=["next"],
                    href=_query(
                        _href(app, request, "resource_page", store=store, resource=resource),
                        cursor=_cursor(scope, str(next_value), [*history, service_cursor]),
                        limit=page_limit,
                    ),
                )
            )
        if history:
            links.append(
                Link(
                    rel=["prev"],
                    href=_query(
                        _href(app, request, "resource_page", store=store, resource=resource),
                        cursor=_cursor(scope, history[-1], history[:-1]),
                        limit=page_limit,
                    ),
                )
            )
        action = Action(
            id="search-resource",
            title=f"Search {resource_name}",
            method="GET",
            href=_query(_href(app, request, "search_page", store=store), resource=resource_name),
            content_type=config.agent_page.content_type,
            input_schema=SearchInput.model_json_schema(),
        )
        items = list(result.get("items", []))
        page = _agent_page(
            config,
            PageIdentity(
                id=self_url,
                type="resource",
                title=resource_name,
                summary=f"Normalized {descriptor.get('kind', 'source')} resource",
            ),
            _public_resource(descriptor),
            links,
            entities=[_record_entity(app, request, store, item) for item in items],
            actions=[action],
            vendor=vendor,
            pagination={
                "limit": page_limit,
                "returned": len(items),
                "total": result.get("total"),
                "has_next_page": next_value is not None,
            },
        )
        return _page_response(app, config, request, page)

    @app.get("/{store}/search", name="search_page")
    def search_page(
        request: Request,
        store: str,
        q: str = Query(min_length=1),
        resource: str | None = None,
        cursor: str | None = None,
        limit: int | None = Query(default=None, ge=1),
    ) -> JSONResponse:
        """Search normalized scalar content and return navigable grounded records."""
        vendor, page_limit = _vendor(catalog, store), _limit(config, limit)
        if len(q) > config.limits.max_query_length:
            raise AgentProblem(
                400, f"Search queries cannot exceed {config.limits.max_query_length} characters."
            )
        vendor_id = _vendor_id(vendor)
        if resource and catalog.get_resource(vendor_id, resource) is None:
            raise AgentProblem(404, "The requested resource was not found.")
        scope = f"search:{vendor_id}:{vendor.get('active_sync_id')}:{resource or '*'}:{q}"
        service_cursor, history = _cursor_state(cursor, scope)
        result = catalog.search_records(
            vendor_id,
            q,
            resource=resource,
            cursor=str(service_cursor) if service_cursor is not None else None,
            limit=page_limit,
        )
        next_value, self_url = result.get("next_cursor"), str(request.url)
        links = [
            Link(rel=["self"], href=self_url),
            Link(rel=["home"], href=_href(app, request, "store_home", store=store)),
        ]
        if resource:
            links.append(
                Link(
                    rel=["collection"],
                    href=_href(
                        app,
                        request,
                        "resource_page",
                        store=store,
                        resource=encode_resource_name(resource),
                    ),
                )
            )
        if next_value is not None:
            links.append(
                Link(
                    rel=["next"],
                    href=_query(
                        _href(app, request, "search_page", store=store),
                        q=q,
                        resource=resource,
                        cursor=_cursor(scope, str(next_value), [*history, service_cursor]),
                        limit=page_limit,
                    ),
                )
            )
        if history:
            links.append(
                Link(
                    rel=["prev"],
                    href=_query(
                        _href(app, request, "search_page", store=store),
                        q=q,
                        resource=resource,
                        cursor=_cursor(scope, history[-1], history[:-1]),
                        limit=page_limit,
                    ),
                )
            )
        items = list(result.get("items", []))
        page = _agent_page(
            config,
            PageIdentity(id=self_url, type="search-results", title=f"Search results for {q}"),
            {"query": q, "resource": resource},
            links,
            entities=[_record_entity(app, request, store, item) for item in items],
            vendor=vendor,
            pagination={
                "limit": page_limit,
                "returned": len(items),
                "total": result.get("total"),
                "has_next_page": next_value is not None,
            },
        )
        return _page_response(app, config, request, page)

    @app.get("/{store}/resources/{resource}/{record_id}", name="record_page")
    def record_page(request: Request, store: str, resource: str, record_id: str) -> JSONResponse:
        """Expose one full normalized record and its provenance without truncation."""
        vendor = _vendor(catalog, store)
        resource_name = _decode_resource_name(resource)
        record = catalog.get_record(_vendor_id(vendor), record_id)
        if record is None or str(record.get("resource")) != resource_name:
            raise AgentProblem(404, "The requested record was not found.")
        self_url = _href(
            app, request, "record_page", store=store, resource=resource, record_id=record_id
        )
        links = [
            Link(rel=["self"], href=self_url),
            Link(
                rel=["collection"],
                href=_href(app, request, "resource_page", store=store, resource=resource),
            ),
            Link(rel=["home"], href=_href(app, request, "store_home", store=store)),
        ]
        for relationship in record.get("relationships", []):
            target, target_id = (
                relationship.get("target_resource"),
                relationship.get("target_id", relationship.get("record_id")),
            )
            if target and target_id:
                links.append(
                    Link(
                        rel=["related"],
                        href=_href(
                            app,
                            request,
                            "record_page",
                            store=store,
                            resource=encode_resource_name(str(target)),
                            record_id=str(target_id),
                        ),
                    )
                )
        commerce = record.get("commerce") if isinstance(record.get("commerce"), Mapping) else {}
        title = str(commerce.get("title", record_id))
        page = _agent_page(
            config,
            PageIdentity(id=self_url, type="record", title=title),
            _public_record(record),
            links,
            vendor=vendor,
        )
        return _page_response(app, config, request, page)

    @app.get("/{store}/schema", name="store_schema")
    def store_schema(request: Request, store: str) -> JSONResponse:
        """Collect observed source schemas without inferring missing meaning."""
        vendor = _vendor(catalog, store)
        resources = catalog.list_resources(_vendor_id(vendor))
        self_url = _href(app, request, "store_schema", store=store)
        links = [
            Link(rel=["self"], href=self_url),
            Link(rel=["home"], href=_href(app, request, "store_home", store=store)),
            Link(rel=["collection"], href=_href(app, request, "resources_page", store=store)),
        ]
        data = {str(item.get("name", "")): item.get("schema", {}) for item in resources}
        page = _agent_page(
            config,
            PageIdentity(
                id=self_url,
                type="store-schema",
                title=f"{vendor.get('name', store)} observed schemas",
            ),
            data,
            links,
            vendor=vendor,
        )
        return _page_response(app, config, request, page)

    @app.get("/{store}/.well-known/ucp", name="ucp_profile")
    def ucp_profile(request: Request, store: str) -> dict[str, Any]:
        """Publish an SDK-validated profile advertising only ready capabilities."""
        vendor = _vendor(catalog, store)
        capabilities: dict[str, Any] = {}
        if _ucp_ready(vendor):
            capabilities = {
                config.ucp.search_capability: [
                    {
                        "version": config.ucp.version,
                        "spec": config.ucp.search_spec,
                        "schema": config.ucp.search_schema,
                    }
                ],
                config.ucp.lookup_capability: [
                    {
                        "version": config.ucp.version,
                        "spec": config.ucp.lookup_spec,
                        "schema": config.ucp.lookup_schema,
                    }
                ],
            }
        search_url = _href(app, request, "ucp_search", store=store)
        endpoint = search_url.removesuffix("/catalog/search")
        profile = {
            "ucp": {
                "version": config.ucp.version,
                "services": {
                    config.ucp.service: [
                        {
                            "version": config.ucp.version,
                            "spec": config.ucp.service_spec,
                            "transport": "rest",
                            "schema": config.ucp.service_schema,
                            "endpoint": endpoint,
                        }
                    ]
                },
                "capabilities": capabilities,
                "payment_handlers": {},
            }
        }
        return _validated(UcpBusinessProfile, profile)

    def require_ucp(vendor: Mapping[str, Any]) -> None:
        """Refuse unadvertised UCP operations until explicit mapping is ready."""
        if not _ucp_ready(vendor):
            raise AgentProblem(
                409,
                "This store needs an explicit commerce mapping before UCP "
                "catalog access is available.",
            )

    @app.post(
        "/{store}/ucp/catalog/search",
        name="ucp_search",
        response_model=SearchResponse,
        response_model_exclude_none=True,
        response_model_exclude_unset=True,
    )
    def ucp_search(store: str, body: SearchRequest) -> dict[str, Any]:
        """Search mapped products and validate the complete response with UCP SDK."""
        vendor = _vendor(catalog, store)
        require_ucp(vendor)
        if body.filters and body.filters.model_dump(exclude_none=True):
            raise AgentProblem(400, "This catalog does not advertise structured filter support.")
        query = body.query or ""
        if len(query) > config.limits.max_query_length:
            raise AgentProblem(
                400, f"Search queries cannot exceed {config.limits.max_query_length} characters."
            )
        page_limit = _limit(config, body.pagination.limit if body.pagination else None)
        token = body.pagination.cursor if body.pagination else None
        resource = _mapping(vendor.get("mapping")).get("resource")
        scope = f"ucp-search:{_vendor_id(vendor)}:{vendor.get('active_sync_id')}:{query}"
        service_cursor, history = _cursor_state(token, scope)
        if query:
            result = catalog.search_records(
                _vendor_id(vendor),
                query,
                resource=resource,
                cursor=str(service_cursor) if service_cursor is not None else None,
                limit=page_limit,
                commerce_only=True,
            )
        else:
            result = catalog.list_records(
                _vendor_id(vendor),
                resource=resource,
                cursor=str(service_cursor) if service_cursor is not None else None,
                limit=page_limit,
                commerce_only=True,
            )
        products = [
            product
            for record in result.get("items", [])
            if (product := _project(catalog, vendor, record))
        ]
        next_value = result.get("next_cursor")
        pagination: dict[str, Any] = {
            "has_next_page": next_value is not None,
            "total_count": result.get("total"),
        }
        if next_value is not None:
            pagination["cursor"] = _cursor(scope, str(next_value), [*history, service_cursor])
        payload = {
            "ucp": _ucp_meta(config, config.ucp.search_capability),
            "products": products,
            "pagination": pagination,
        }
        return _validated(SearchResponse, payload)

    @app.post(
        "/{store}/ucp/catalog/lookup",
        name="ucp_lookup",
        response_model=LookupResponse,
        response_model_exclude_none=True,
        response_model_exclude_unset=True,
    )
    def ucp_lookup(store: str, body: LookupRequest) -> dict[str, Any]:
        """Resolve a bounded identifier batch with UCP correlation and outcomes."""
        vendor = _vendor(catalog, store)
        require_ucp(vendor)
        if len(body.ids) > config.limits.max_page_size:
            raise AgentProblem(
                400, f"Lookup batches cannot exceed {config.limits.max_page_size} identifiers."
            )
        if body.filters and body.filters.model_dump(exclude_none=True):
            raise AgentProblem(400, "This catalog does not advertise structured filter support.")
        grouped: dict[str, tuple[dict[str, Any], list[str]]] = {}
        missing = []
        for identifier in body.ids:
            product = _find_product(catalog, config, vendor, identifier)
            if product is None:
                missing.append(identifier)
                continue
            key = str(product["id"])
            grouped.setdefault(key, (product, []))[1].append(identifier)
        products = [_lookup_product(product, inputs) for product, inputs in grouped.values()]
        messages = [{"type": "info", "code": "not_found", "content": item} for item in missing]
        payload: dict[str, Any] = {
            "ucp": _ucp_meta(config, config.ucp.lookup_capability),
            "products": products,
        }
        if messages:
            payload["messages"] = messages
        return _validated(LookupResponse, payload)

    @app.post(
        "/{store}/ucp/catalog/product",
        name="ucp_product",
        response_model=GetProductResponse | UcpErrorResponse,
        response_model_exclude_none=True,
        response_model_exclude_unset=True,
    )
    def ucp_product(store: str, body: GetProductRequest) -> dict[str, Any]:
        """Return one full product or an SDK-validated UCP business error."""
        vendor = _vendor(catalog, store)
        require_ucp(vendor)
        if body.filters and body.filters.model_dump(exclude_none=True):
            raise AgentProblem(400, "This catalog does not advertise structured filter support.")
        if body.selected or body.preferences:
            return _ucp_error(config, body.id, "Option selection is unavailable for this product")
        product = _find_product(catalog, config, vendor, body.id)
        if product is None:
            return _ucp_error(config, body.id, "Product not found")
        payload = {"ucp": _ucp_meta(config, config.ucp.lookup_capability), "product": product}
        return _validated(GetProductResponse, payload)

    return app
