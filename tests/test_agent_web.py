"""Focused contract tests for the nested agent-native storefront."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ucp_sdk.models.schemas.common.types.error_response import ErrorResponse
from ucp_sdk.models.schemas.profile import BusinessSchema
from ucp_sdk.models.schemas.shopping.catalog_lookup import (
    GetProductResponse,
    LookupResponse,
)
from ucp_sdk.models.schemas.shopping.catalog_search import SearchResponse

from agent_web import create_agent_app
from config import CommerceConfig


class FakeCatalog:
    """Provide deterministic in-memory data through the real catalog protocol."""

    def __init__(self) -> None:
        """Create mapped and incomplete stores over the same lossless fixtures."""
        fields = {
            "id": "sku",
            "title": "name",
            "description": "description",
            "price": "price",
            "currency": "currency",
        }
        self.vendors = {
            "mapped": self._vendor(
                "mapped",
                "Mapped Store",
                {"resource": "products", "fields": fields, "price_units": "minor"},
            ),
            "incomplete": self._vendor(
                "incomplete",
                "Raw Store",
                {"resource": "products", "fields": {"id": "sku", "title": "name"}},
            ),
        }
        self.resources = [
            {"name": "products", "kind": "table", "schema": {"type": "object"}, "record_count": 3},
            {"name": "inventory", "kind": "table", "schema": {"type": "object"}, "record_count": 1},
        ]
        self.records = [
            self._record(
                "r1", "products", "p-blue", "Blue Runner", "Road shoe", 12000, {"color": "blue"}
            ),
            self._record(
                "r2", "products", "p-trail", "Trail Runner", "Trail shoe", 15000, {"color": "green"}
            ),
            self._record(
                "r3", "products", "p-mug", "Camp Mug", "Steel mug", 2400, {"volume_ml": 350}
            ),
            {
                "_id": "r4",
                "resource": "inventory",
                "data": {"sku": "p-blue", "warehouse": "north", "quantity": 7},
                "relationships": [],
            },
        ]

    @staticmethod
    def _vendor(slug: str, name: str, mapping: dict[str, Any]) -> dict[str, Any]:
        """Build a serialized public vendor document."""
        return {
            "_id": f"vendor-{slug}",
            "slug": slug,
            "name": name,
            "public": True,
            "status": "ready",
            "active_sync_id": "sync-1",
            "mapping": mapping,
        }

    @staticmethod
    def _record(
        identifier: str,
        resource: str,
        sku: str,
        name: str,
        description: str,
        price: int,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a lossless normalized record with an arbitrary nested field."""
        data = {
            "sku": sku,
            "name": name,
            "description": description,
            "price": price,
            "currency": "USD",
            "extra": extra,
        }
        return {
            "_id": identifier,
            "resource": resource,
            "data": data,
            "relationships": [],
            "commerce": None,
            "sync_id": "sync-1",
        }

    def get_vendor_by_slug(self, slug: str, public_only: bool = False) -> dict[str, Any] | None:
        """Resolve a public vendor by its external slug."""
        vendor = self.vendors.get(slug)
        return vendor if vendor and (not public_only or vendor["public"]) else None

    def list_resources(self, _vendor_id: str, sync_id: str | None = None) -> list[dict[str, Any]]:
        """Return observed resources in deterministic order."""
        return list(self.resources)

    def get_resource(
        self, _vendor_id: str, name: str, sync_id: str | None = None
    ) -> dict[str, Any] | None:
        """Resolve one observed resource by exact name."""
        return next((item for item in self.resources if item["name"] == name), None)

    def list_records(
        self,
        _vendor_id: str,
        resource: str | None = None,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        query: str | None = None,
        commerce_only: bool = False,
    ) -> dict[str, Any]:
        """Page records using a service cursor hidden by the web layer."""
        items = [item for item in self.records if resource is None or item["resource"] == resource]
        return self._page(items, cursor, limit)

    def get_record(self, _vendor_id: str, record_id: str) -> dict[str, Any] | None:
        """Resolve either a stable normalized ID or mapped product ID."""
        return next(
            (
                item
                for item in self.records
                if item["_id"] == record_id or item["data"].get("sku") == record_id
            ),
            None,
        )

    def search_records(
        self,
        _vendor_id: str,
        query: str,
        *,
        resource: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        commerce_only: bool = False,
    ) -> dict[str, Any]:
        """Search serialized source values without commerce inference."""
        candidates = [
            item for item in self.records if resource is None or item["resource"] == resource
        ]
        matches = [
            item for item in candidates if query.casefold() in json.dumps(item["data"]).casefold()
        ]
        return self._page(matches, cursor, limit)

    def project_record(
        self, record: Mapping[str, Any], mapping: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Project only fields named by the explicit merchant mapping."""
        fields, data = mapping.get("fields", {}), record["data"]
        required = ("id", "title", "description", "price")
        if not all(key in fields and fields[key] in data for key in required):
            return None
        currency = data.get(fields.get("currency")) or mapping.get("default_currency")
        if mapping.get("price_units") != "minor" or not currency:
            return None
        return {
            "id": data[fields["id"]],
            "title": data[fields["title"]],
            "description": data[fields["description"]],
            "price": data[fields["price"]],
            "currency": currency,
        }

    @staticmethod
    def _page(items: list[dict[str, Any]], cursor: str | None, limit: int | None) -> dict[str, Any]:
        """Return one deterministic cursor page."""
        offset, size = int(cursor or 0), limit or 20
        selected = items[offset : offset + size]
        next_cursor = str(offset + size) if offset + size < len(items) else None
        return {"items": selected, "next_cursor": next_cursor, "total": len(items)}


@pytest.fixture
def commerce_config() -> CommerceConfig:
    """Load the same validated non-secret configuration as production."""
    path = Path(__file__).resolve().parents[1] / "config" / "commerce.yml"
    return CommerceConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@pytest.fixture
def client(commerce_config: CommerceConfig) -> TestClient:
    """Mount the child application so root-path URL generation is exercised."""
    outer = FastAPI()
    outer.mount(commerce_config.routes.agent, create_agent_app(commerce_config, FakeCatalog()))
    return TestClient(outer, raise_server_exceptions=False)


def _link(page: Mapping[str, Any], relation: str) -> str:
    """Select one advertised transition by link relation."""
    return next(link["href"] for link in page["links"] if relation in link["rel"])


def _cursor_from(href: str) -> str:
    """Extract an opaque cursor exactly as a client would return it."""
    return parse_qs(urlparse(href).query)["cursor"][0]


def test_generic_pages_are_traversable_from_home(client: TestClient) -> None:
    """Reach search, a resource, and a full record using advertised links only."""
    home_response = client.get("/agent/mapped/")
    assert home_response.status_code == 200
    assert (
        'profile="http://testserver/agent/schema/page.json"'
        in home_response.headers["content-type"]
    )
    home = home_response.json()
    assert all(link["href"].startswith("http://testserver/agent/mapped") for link in home["links"])

    resource = client.get(home["entities"][0]["href"], params={"limit": 1}).json()
    assert resource["entities"][0]["data"]["extra"] == {"color": "blue"}
    record = client.get(resource["entities"][0]["href"]).json()
    assert record["data"]["data"]["sku"] == "p-blue"


def test_unsafe_resource_names_round_trip_through_one_path_segment(
    commerce_config: CommerceConfig,
) -> None:
    """Encode slashes and Unicode without changing the source resource identity."""
    catalog = FakeCatalog()
    resource_name = "sizes/été"
    catalog.resources.append(
        {"name": resource_name, "kind": "list", "schema": {}, "record_count": 1}
    )
    catalog.records.append(
        {
            "_id": "r-safe",
            "resource": resource_name,
            "data": {"label": "retained"},
            "relationships": [],
        }
    )
    outer = FastAPI()
    outer.mount(commerce_config.routes.agent, create_agent_app(commerce_config, catalog))

    with TestClient(outer, raise_server_exceptions=False) as client:
        home = client.get("/agent/mapped/").json()
        resource_link = next(
            item["href"] for item in home["entities"] if item["id"] == resource_name
        )
        resource = client.get(resource_link).json()
        record = client.get(resource["entities"][0]["href"]).json()

    assert resource["page"]["title"] == resource_name
    assert record["data"]["data"] == {"label": "retained"}

    search_url = next(action["href"] for action in home["actions"] if action["id"] == "search")
    search = client.get(search_url, params={"q": "Camp Mug"}).json()
    assert [entity["id"] for entity in search["entities"]] == ["r3"]
    assert client.get(_link(home, "schema")).json()["page"]["type"] == "store-schema"


def test_generic_cursor_pages_have_no_duplicates_and_can_go_back(client: TestClient) -> None:
    """Keep service positions opaque while supporting stable next and previous traversal."""
    first = client.get("/agent/mapped/resources/products", params={"limit": 1}).json()
    next_url = _link(first, "next")
    assert _cursor_from(next_url) != "1"

    second = client.get(next_url).json()
    assert first["entities"][0]["id"] != second["entities"][0]["id"]
    previous = client.get(_link(second, "prev")).json()
    assert previous["entities"][0]["id"] == first["entities"][0]["id"]

    resources = client.get("/agent/mapped/resources", params={"limit": 1}).json()
    later_resources = client.get(_link(resources, "next")).json()
    assert resources["entities"][0]["name"] == "products"
    assert later_resources["entities"][0]["name"] == "inventory"


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/agent/unknown/", 404),
        ("/agent/mapped/resources?cursor=not-a-cursor", 400),
        ("/agent/mapped/search", 422),
        ("/agent/mapped/resources/products/missing", 404),
    ],
)
def test_failures_use_safe_rfc9457_problems(client: TestClient, path: str, status: int) -> None:
    """Return consistent problem documents without exception or persistence details."""
    response = client.get(path)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json().keys() == {"type", "title", "status", "detail", "instance"}
    assert response.json()["status"] == status
    assert "traceback" not in response.text.casefold()


def test_ucp_discovery_is_conditional_and_sdk_valid(client: TestClient) -> None:
    """Advertise exactly search and lookup only for a completely mapped store."""
    mapped = client.get("/agent/mapped/.well-known/ucp").json()
    BusinessSchema.model_validate(mapped)
    assert set(mapped["ucp"]["capabilities"]) == {
        "dev.ucp.shopping.catalog.search",
        "dev.ucp.shopping.catalog.lookup",
    }
    assert mapped["ucp"]["payment_handlers"] == {}
    assert (
        mapped["ucp"]["services"]["dev.ucp.shopping"][0]["endpoint"]
        == "http://testserver/agent/mapped/ucp"
    )

    incomplete = client.get("/agent/incomplete/.well-known/ucp").json()
    BusinessSchema.model_validate(incomplete)
    assert incomplete["ucp"]["capabilities"] == {}
    assert client.get("/agent/incomplete/").json()["data"]["ucp_catalog"] == "needs_mapping"


def test_ucp_search_paginates_and_validates_with_official_sdk(client: TestClient) -> None:
    """Validate projected search products and return the issued cursor unchanged."""
    first_response = client.post(
        "/agent/mapped/ucp/catalog/search",
        json={"query": "runner", "pagination": {"limit": 1}},
    )
    first = SearchResponse.model_validate(first_response.json())
    assert first_response.status_code == 200
    assert first.products[0].id == "p-blue"
    assert first.products[0].price_range.min.amount == 12000
    assert first.pagination and first.pagination.has_next_page

    second_response = client.post(
        "/agent/mapped/ucp/catalog/search",
        json={"query": "runner", "pagination": {"limit": 1, "cursor": first.pagination.cursor}},
    )
    second = SearchResponse.model_validate(second_response.json())
    assert second.products[0].id == "p-trail"
    assert {first.products[0].id, second.products[0].id} == {"p-blue", "p-trail"}


def test_ucp_lookup_product_and_business_error_validate(client: TestClient) -> None:
    """Validate partial lookup, detail, and not-found business outcomes through the SDK."""
    lookup_response = client.post(
        "/agent/mapped/ucp/catalog/lookup", json={"ids": ["p-blue", "missing"]}
    )
    lookup = LookupResponse.model_validate(lookup_response.json())
    assert lookup_response.status_code == 200
    assert [product.id for product in lookup.products] == ["p-blue"]
    assert lookup.products[0].variants[0].inputs[0].id == "p-blue"
    assert lookup.messages and lookup.messages[0].content == "missing"

    detail_response = client.post("/agent/mapped/ucp/catalog/product", json={"id": "p-blue"})
    detail = GetProductResponse.model_validate(detail_response.json())
    assert detail.product.id == "p-blue"
    assert detail.product.variants[0].id == "p-blue::default"

    missing_response = client.post("/agent/mapped/ucp/catalog/product", json={"id": "missing"})
    missing = ErrorResponse.model_validate(missing_response.json())
    assert missing_response.status_code == 200
    assert missing.ucp.status == "error"
    assert missing.messages[0].code == "not_found"


def test_incomplete_mapping_keeps_generic_data_but_blocks_ucp(client: TestClient) -> None:
    """Preserve raw browsing while refusing to invent an incomplete commerce projection."""
    generic = client.get("/agent/incomplete/resources/products/r1")
    assert generic.status_code == 200
    assert generic.json()["data"]["data"]["extra"] == {"color": "blue"}

    ucp = client.post("/agent/incomplete/ucp/catalog/search", json={"query": "runner"})
    assert ucp.status_code == 409
    assert ucp.headers["content-type"].startswith("application/problem+json")
    assert "mapping" in ucp.json()["detail"].casefold()
