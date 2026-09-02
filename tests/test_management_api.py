"""Outer application integration tests against one isolated real Mongo database."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo import MongoClient

from config import Settings, get_settings
from main import create_app
from model_layer.client import ProviderDecision
from models import BrowserDecision

_DATABASE_PREFIX = "commerceos_management_pytest_"


@pytest.fixture(scope="module")
def integration_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Settings, MongoClient[dict[str, Any]], Path, str]:
    """Yield one UUID-named real database and drop only that verified database."""
    source_root = tmp_path_factory.mktemp("commerceos-management-sources")
    base = get_settings()
    database_name = f"{_DATABASE_PREFIX}{uuid4().hex}"
    UUID(database_name.removeprefix(_DATABASE_PREFIX))
    mongo: MongoClient[dict[str, Any]] = MongoClient(
        base.mongodb_uri,
        serverSelectionTimeoutMS=base.commerce.limits.mongo_timeout_milliseconds,
    )
    settings = Settings(
        config_path=base.config_path,
        mongodb_uri=base.mongodb_uri,
        mongodb_database=database_name,
        source_roots=[source_root],
        app_env="test",
        app_host="127.0.0.1",
        app_port=8000,
        admin_api_key=None,
        model_provider=None,
        model_name=None,
        model_api_base=None,
        razorpay_enabled=False,
    )
    try:
        mongo.admin.command("ping")
        yield settings, mongo, source_root, database_name
    finally:
        assert database_name.startswith(_DATABASE_PREFIX)
        UUID(database_name.removeprefix(_DATABASE_PREFIX))
        mongo.drop_database(database_name)
        assert database_name not in mongo.list_database_names()
        mongo.close()


@pytest.fixture(scope="module")
def client(
    integration_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str],
) -> TestClient:
    """Run the real application factory with lifespan, indexes, and injected storage."""
    settings, mongo, _, _ = integration_environment
    with TestClient(create_app(settings, mongo), raise_server_exceptions=False) as http:
        yield http


def _assert_problem(response: Any, status: int) -> dict[str, Any]:
    """Validate the stable RFC 9457 management error boundary."""
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem.keys() == {"type", "title", "status", "detail", "instance"}
    assert problem["type"] == "about:blank"
    assert problem["status"] == status
    assert problem["instance"].startswith("http://testserver/")
    assert "traceback" not in response.text.casefold()
    return problem


def _write_catalog(source_root: Path) -> Path:
    """Create a complete deterministic CSV inside the configured allowed root."""
    source = source_root / f"products-{uuid4().hex[:8]}.csv"
    source.write_text(
        "sku,title,description,price,currency,category,arbitrary_note\n"
        "p-moss,Moss Lamp,Warm desk light,1299,USD,Lighting,retained exactly\n"
        "p-mint,Mint Mug,Stoneware cup,2400,USD,Kitchen,seasonal detail\n"
        "p-trail,Trail Pack,Weatherproof daypack,8900,USD,Outdoor,hidden pocket\n",
        encoding="utf-8",
    )
    return source


def _write_stocked_catalog(source_root: Path) -> Path:
    """Create the multi-product fixture used for purchase-boundary tests."""
    source = source_root / f"shop-{uuid4().hex[:8]}.csv"
    source.write_text(
        "sku,name,description,brand,price,currency,stock_quantity,availability\n"
        "FAS-001,Classic Canvas Sneakers,Everyday sneakers,Northline,59.95,USD,33,in_stock\n"
        "FAS-002,Waterproof Hiking Jacket,Outdoor shell,Trailmark,119.00,USD,9,low_stock\n"
        "SPT-001,Adjustable Yoga Mat,Exercise mat,FlexForm,39.99,USD,27,in_stock\n",
        encoding="utf-8",
    )
    return source


def _publish_shop(client: TestClient, source_root: Path) -> tuple[str, dict[str, Any]]:
    """Register, sync, and map a stocked catalog, then index records by SKU."""
    source = _write_stocked_catalog(source_root)
    created = client.post(
        "/api/vendors",
        json={
            "name": "Purchase Shop",
            "slug": f"purchase-shop-{uuid4().hex[:8]}",
            "source": {"kind": "csv", "path": str(source)},
            "public": True,
        },
    )
    assert created.status_code == 201
    vendor_id = created.json()["vendor"]["_id"]
    assert client.post(f"/api/vendors/{vendor_id}/sync").status_code == 200
    mapped = client.put(
        f"/api/vendors/{vendor_id}/mapping",
        json={
            "mapping": {
                "resource": source.stem,
                "fields": {
                    "id": "sku",
                    "title": "name",
                    "description": "description",
                    "price": "price",
                    "currency": "currency",
                    "brand": "brand",
                    "availability": "availability",
                    "inventory": "stock_quantity",
                },
                "price_units": "major",
            }
        },
    )
    assert mapped.status_code == 200
    records = client.get(f"/api/vendors/{vendor_id}/records", params={"resource": source.stem})
    assert records.status_code == 200
    by_sku = {item["data"]["sku"]: item for item in records.json()["items"]}
    return vendor_id, by_sku


def _stock_values(record: dict[str, Any]) -> tuple[Any, Any]:
    """Read source and projected inventory without treating either as a reservation."""
    return record["data"]["stock_quantity"], record.get("commerce", {}).get("inventory")


def test_dashboard_static_health_and_readiness(client: TestClient) -> None:
    """Serve the consolidated dashboard assets and distinguish health from readiness."""
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert dashboard.headers["content-type"].startswith("text/html")
    assert "CommerceOS" in dashboard.text
    assert 'href="/static/app.css"' in dashboard.text
    assert 'src="/static/app.js"' in dashboard.text

    stylesheet = client.get("/static/app.css")
    script = client.get("/static/app.js")
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert "--moss:" in stylesheet.text
    assert script.status_code == 200
    assert "function apiUrl" in script.text
    assert "function selectProduct" in script.text
    assert "function changeQuantity" in script.text
    assert "function removeSelectedProduct" in script.text
    assert "selectedProducts" in script.text
    assert "state.selectedProducts = []" in script.text
    assert "Select product" in script.text
    assert "Selected items" in script.text
    assert "item.local" in script.text
    assert "length >= 2" not in script.text
    assert "length > 2" not in script.text
    assert "maxSelected" not in script.text
    assert "RAZORPAY_KEY_SECRET" not in script.text
    assert "webhook_secret" not in script.text
    assert "Confirm purchase and continue to payment" in script.text
    assert "function addToCart" in script.text
    assert "/orders" not in script.text
    assert ".product-pick" in stylesheet.text
    assert ".product-qty" in stylesheet.text
    assert ".purchase-line" in stylesheet.text
    for name in ("selectProduct", "changeQuantity", "removeSelectedProduct", "addToCart"):
        body = script.text.split(f"function {name}", 1)[1].split("\nfunction ", 1)[0]
        assert "fetchJson" not in body
        assert "fetch(" not in body
        assert "/inventory" not in body
        assert "RAZORPAY_KEY_SECRET" not in body

    health = client.get("/api/health")
    ready = client.get("/api/ready")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["name"] == "CommerceOS"
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_vendor_sync_mapping_records_and_grounded_chat(
    client: TestClient,
    integration_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str],
) -> None:
    """Exercise the complete management flow and follow its deterministic citation."""
    _, _, source_root, _ = integration_environment
    source = _write_catalog(source_root)
    slug = f"integration-store-{uuid4().hex[:8]}"
    created = client.post(
        "/api/vendors",
        json={
            "name": "Integration Store",
            "slug": slug,
            "source": {"kind": "csv", "path": str(source)},
            "public": True,
        },
    )
    assert created.status_code == 201
    vendor = created.json()["vendor"]
    vendor_id = vendor["_id"]
    assert vendor["slug"] == slug
    assert vendor["status"] == "registered"

    listed = client.get("/api/vendors")
    assert listed.status_code == 200
    assert any(item["_id"] == vendor_id for item in listed.json()["items"])
    detail = client.get(f"/api/vendors/{vendor_id}")
    assert detail.status_code == 200
    assert detail.json()["vendor"]["source"] == {"kind": "csv", "path": str(source)}
    assert detail.json()["stats"]["records"] == 0

    synchronized = client.post(f"/api/vendors/{vendor_id}/sync")
    assert synchronized.status_code == 200
    sync = synchronized.json()["sync"]
    assert sync["status"] == "succeeded"
    assert sync["counts"] == {
        "resources": 1,
        "records": 3,
        "written": 3,
        "projected": 0,
        "warnings": 0,
    }
    assert sync["resources"] == 1
    assert sync["records"] == 3
    assert sync["warning_count"] == 0
    assert sync["warnings"] == []
    assert client.get(f"/api/vendors/{vendor_id}").json()["vendor"]["status"] == "needs_mapping"

    history = client.get(f"/api/vendors/{vendor_id}/syncs")
    assert history.status_code == 200
    assert history.json()["items"][0]["sync_id"] == sync["sync_id"]
    assert history.json()["items"][0]["counts"]["records"] == 3
    resources = client.get(f"/api/vendors/{vendor_id}/resources")
    assert resources.status_code == 200
    resource = resources.json()["items"][0]
    assert resource["name"] == source.stem
    assert resource["record_count"] == 3
    assert resource["mapping_suggestions"]["description"]["field"] == "description"

    first_page = client.get(
        f"/api/vendors/{vendor_id}/records",
        params={"resource": source.stem, "limit": 2},
    )
    assert first_page.status_code == 200
    assert first_page.json()["total"] == 3
    assert len(first_page.json()["items"]) == 2
    assert first_page.json()["next_cursor"]
    second_page = client.get(
        f"/api/vendors/{vendor_id}/records",
        params={
            "resource": source.stem,
            "limit": 2,
            "cursor": first_page.json()["next_cursor"],
        },
    )
    first_ids = {item["_id"] for item in first_page.json()["items"]}
    second_ids = {item["_id"] for item in second_page.json()["items"]}
    assert len(second_ids) == 1
    assert first_ids.isdisjoint(second_ids)
    assert {item["data"]["arbitrary_note"] for item in first_page.json()["items"]} <= {
        "retained exactly",
        "seasonal detail",
        "hidden pocket",
    }

    fields = {
        "id": "sku",
        "title": "title",
        "description": "description",
        "price": "price",
        "currency": "currency",
        "category": "category",
    }
    mapped = client.put(
        f"/api/vendors/{vendor_id}/mapping",
        json={"mapping": {"resource": source.stem, "fields": fields, "price_units": "minor"}},
    )
    assert mapped.status_code == 200
    assert mapped.json()["vendor"]["status"] == "ready"
    assert mapped.json()["vendor"]["mapping"]["fields"] == fields

    records = client.get(
        f"/api/vendors/{vendor_id}/records",
        params={"resource": source.stem, "q": "Moss Lamp"},
    )
    assert records.status_code == 200
    assert records.json()["total"] == 1
    assert records.json()["items"][0]["commerce"] == {
        "id": "p-moss",
        "title": "Moss Lamp",
        "description": "Warm desk light",
        "price": 1299,
        "currency": "USD",
        "category": "Lighting",
    }

    chat = client.post(
        f"/api/vendors/{vendor_id}/chat",
        json={"message": "Moss Lamp", "history": []},
    )
    assert chat.status_code == 200
    answer = chat.json()
    assert answer["mode"] == "deterministic"
    assert "Moss Lamp" in answer["answer"]
    assert len(answer["sources"]) == 1
    citation = answer["sources"][0]
    assert citation["label"].startswith(f"{source.stem}/")
    assert citation["title"] == "Moss Lamp"
    assert citation["href"].startswith(f"http://testserver/agent/{slug}/resources/{source.stem}/")
    assert citation["product"]["name"] == "Moss Lamp"
    assert citation["product"]["id"] == "p-moss"
    assert citation["product"]["sku"] == "p-moss"
    assert citation["product"]["record_id"]
    assert citation["product"]["record_id"] != "Moss Lamp"
    assert citation["product"]["price"] == pytest.approx(12.99)
    assert citation["product"]["currency"] == "USD"
    assert "/agent/" not in answer["answer"]
    cited_page = client.get(citation["href"])
    assert cited_page.status_code == 200
    assert cited_page.json()["data"]["data"]["sku"] == "p-moss"

    visited: list[str] = []
    opened_records: list[str] = []

    class ScriptedBrowserModel:
        """Choose controls from each real page to prove the complete browsing loop."""

        async def acompletion(self, **kwargs: Any) -> Any:
            """Return a validated decision based only on the supplied current page."""
            prompt = kwargs["messages"][-1]["content"]
            current = prompt.split("<current-agent-page>", 1)[1].split("</current-agent-page>", 1)[
                0
            ]
            page = json.loads(current)
            page_type = page["page"]["type"]
            visited.append(page_type)
            assert kwargs["response_format"] is ProviderDecision
            if page_type == "store":
                decision = BrowserDecision(
                    operation="submit",
                    target="search",
                    inputs={"q": "Moss Lamp Mint Mug", "limit": 5},
                )
            elif page_type == "search-results":
                entity = next(
                    item for item in page["entities"] if item["commerce"]["title"] == "Moss Lamp"
                )
                decision = BrowserDecision(operation="follow", target=entity["href"])
            elif page_type == "record" and page["page"]["title"] == "Moss Lamp":
                opened_records.append(page["page"]["id"])
                collection = next(
                    item["href"] for item in page["links"] if "collection" in item["rel"]
                )
                decision = BrowserDecision(operation="follow", target=collection)
            elif page_type == "resource":
                entity = next(
                    item for item in page["entities"] if item["commerce"]["title"] == "Mint Mug"
                )
                decision = BrowserDecision(operation="follow", target=entity["href"])
            else:
                opened_records.append(page["page"]["id"])
                decision = BrowserDecision(
                    operation="answer",
                    answer=(
                        "The Moss Lamp is a warm desk light priced at $12.99, while the Mint "
                        "Mug is a stoneware cup priced at $24.00. Choose the lamp for lighting "
                        "a workspace and the mug for drinkware; they serve different needs."
                    ),
                    citations=opened_records,
                )
            message = SimpleNamespace(parsed=decision, content=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    browser = client.app.state.agent_browser
    browser.model.client = ScriptedBrowserModel()
    try:
        agent_chat = client.post(
            f"/api/vendors/{vendor_id}/chat",
            json={"message": "Compare the Moss Lamp and Mint Mug.", "history": []},
        )
    finally:
        browser.model.client = None

    assert agent_chat.status_code == 200
    agent_answer = agent_chat.json()
    assert agent_answer["mode"] == "agent"
    assert "$12.99" in agent_answer["answer"] and "$24.00" in agent_answer["answer"]
    assert "/agent/" not in agent_answer["answer"]
    assert visited == ["store", "search-results", "record", "resource", "record"]
    assert [item["page_type"] for item in agent_answer["trace"]] == visited
    assert len(agent_answer["sources"]) == 2
    assert {item["title"] for item in agent_answer["sources"]} == {"Moss Lamp", "Mint Mug"}
    assert all("/agent/" in item["href"] for item in agent_answer["sources"])
    assert all(client.get(item["href"]).status_code == 200 for item in agent_answer["sources"])

    final_detail = client.get(f"/api/vendors/{vendor_id}").json()
    assert final_detail["stats"]["resources"] == 1
    assert final_detail["stats"]["records"] == 3
    assert final_detail["stats"]["active_sync_id"] == sync["sync_id"]


def test_purchase_review_authorize_and_cancel_do_not_mutate_inventory(
    client: TestClient,
    integration_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str],
) -> None:
    """Rebuild totals from catalog data and keep stock unchanged until verified payment."""
    _, mongo, source_root, database_name = integration_environment
    vendor_id, by_sku = _publish_shop(client, source_root)
    sneakers, jacket, mat = by_sku["FAS-001"], by_sku["FAS-002"], by_sku["SPT-001"]
    before = {sku: _stock_values(item) for sku, item in by_sku.items()}

    review = client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={
            "items": [
                {"record_id": sneakers["_id"], "quantity": 2, "displayed_price": 0.01},
                {"record_id": jacket["_id"], "quantity": 1, "displayed_price": 119},
                {"record_id": mat["_id"], "quantity": 3},
            ]
        },
    )
    assert review.status_code == 200
    purchase = review.json()["purchase"]
    assert purchase["status"] == "review"
    assert [item["name"] for item in purchase["items"]] == [
        "Classic Canvas Sneakers",
        "Waterproof Hiking Jacket",
        "Adjustable Yoga Mat",
    ]
    assert [item["quantity"] for item in purchase["items"]] == [2, 1, 3]
    assert purchase["items"][0]["unit_price"] == pytest.approx(59.95)
    assert purchase["items"][0]["subtotal"] == pytest.approx(119.90)
    assert purchase["items"][1]["subtotal"] == pytest.approx(119.00)
    assert purchase["items"][2]["subtotal"] == pytest.approx(119.97)
    assert purchase["total"] == pytest.approx(358.87)
    assert purchase["currency"] == "USD"
    assert purchase["payment"]["started"] is False
    assert purchase["payment"]["succeeded"] is False
    assert purchase["payment"]["failed"] is False
    dumped = json.dumps(purchase["items"])
    assert "record_id" not in dumped
    assert sneakers["_id"] not in dumped
    assert "FAS-001" not in dumped
    body = review.text.casefold()
    assert "razorpay" not in body
    assert "api_key" not in body
    assert "traceback" not in body
    assert any("snapshot" in notice.lower() for notice in purchase["notices"])

    denied = client.post(
        f"/api/vendors/{vendor_id}/purchases/{purchase['id']}/authorize",
        json={"confirm": False},
    )
    problem = _assert_problem(denied, 400)
    assert "explicit" in problem["detail"].casefold()

    purchases = mongo[database_name]["purchase_attempts"]
    review_count = purchases.count_documents({})
    chat = client.post(
        f"/api/vendors/{vendor_id}/chat",
        json={"message": "I want to buy the sneakers.", "history": []},
    )
    assert chat.status_code == 200
    assert chat.json()["mode"] == "deterministic"
    assert purchases.count_documents({}) == review_count
    assert purchases.count_documents({"status": "authorized"}) == 0

    missing = client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": "FAS-001", "quantity": 1}]},
    )
    _assert_problem(missing, 404)

    stocked = client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": sneakers["_id"], "quantity": 5}]},
    )
    assert stocked.status_code == 200
    too_many = client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": sneakers["_id"], "quantity": 40}]},
    )
    problem = _assert_problem(too_many, 400)
    assert "Only 33 Classic Canvas Sneakers are currently available" in problem["detail"]

    authorized = client.post(
        f"/api/vendors/{vendor_id}/purchases/{purchase['id']}/authorize",
        json={"confirm": True},
    )
    assert authorized.status_code == 200
    result = authorized.json()["purchase"]
    assert result["status"] == "authorized"
    assert result["payment"]["started"] is False
    assert result["payment"]["succeeded"] is False
    assert "inventory was not changed" in authorized.json()["message"].casefold()
    assert "razorpay" not in authorized.text.casefold()

    cancelled = client.post(f"/api/vendors/{vendor_id}/purchases/{purchase['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["purchase"]["status"] == "cancelled"
    assert cancelled.json()["purchase"]["payment"]["succeeded"] is False

    refreshed = client.get(
        f"/api/vendors/{vendor_id}/records",
        params={"resource": sneakers["resource"]},
    )
    after = {item["data"]["sku"]: _stock_values(item) for item in refreshed.json()["items"]}
    assert after == before
    stored = purchases.find_one({"attempt_id": purchase["id"]})
    assert stored["payment"]["started"] is False
    assert stored["payment"]["succeeded"] is False
    assert stored["items"][0]["unit_price_minor"] == 5995
    assert stored["items"][0]["displayed_price_minor"] == 1


@pytest.mark.parametrize(
    ("method", "path", "payload", "status"),
    [
        ("post", "/api/vendors", {"name": "", "source": {"kind": "", "path": ""}}, 422),
        ("get", "/api/vendors/missing-storefront", None, 404),
        ("get", "/api/vendors/missing-storefront/records?limit=0", None, 422),
    ],
)
def test_invalid_and_missing_management_responses_are_rfc9457(
    client: TestClient,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    status: int,
) -> None:
    """Translate invalid inputs and absent resources into safe problem documents."""
    response = (
        client.request(method, path, json=payload)
        if payload is not None
        else client.request(method, path)
    )
    problem = _assert_problem(response, status)
    assert problem["detail"]


def test_request_body_limit_counts_actual_payload_bytes(client: TestClient) -> None:
    """Reject oversized JSON before validation through Starlette's maintained middleware."""
    limit = get_settings().commerce.limits.max_request_bytes
    response = client.post(
        "/api/vendors",
        content=b"x" * (limit + 1),
        headers={"content-type": "application/json", "content-length": "1"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Content Too Large"}


def test_optional_admin_api_key_protects_management_only(
    integration_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str],
) -> None:
    """Enforce an explicitly configured operator key while keeping probes reachable."""
    settings, mongo, _, _ = integration_environment
    key = "management-integration-secret"
    protected_settings = settings.model_copy(update={"admin_api_key": SecretStr(key)})
    header = settings.commerce.security.admin_header
    with TestClient(
        create_app(protected_settings, mongo), raise_server_exceptions=False
    ) as protected:
        assert protected.get("/").status_code == 200
        assert protected.get("/api/health").status_code == 200
        assert protected.get("/api/ready").status_code == 200
        _assert_problem(protected.get("/api/vendors"), 401)
        _assert_problem(protected.get("/api/vendors", headers={header: "wrong"}), 401)
        assert protected.get("/api/vendors", headers={header: key}).status_code == 200

    production_without_key = settings.model_copy(
        update={"app_env": "production", "admin_api_key": None}
    )
    with pytest.raises(RuntimeError, match="ADMIN_API_KEY"):
        create_app(production_without_key, mongo)


def test_configured_model_construction_failure_returns_safe_chat_problem(
    monkeypatch: pytest.MonkeyPatch,
    integration_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str],
) -> None:
    """Keep chat usable as an API when AnyLLM cannot start, without leaking provider errors."""
    settings, mongo, source_root, _ = integration_environment
    source = _write_catalog(source_root)

    def create(_provider: str, **_options: Any) -> None:
        """Fail like a missing provider key without constructing a real client."""
        raise RuntimeError("live-credential-must-not-appear")

    monkeypatch.setattr("model_layer.client.AnyLLM.create", create)
    configured = settings.model_copy(
        update={"model_provider": "gemini", "model_name": "gemini-3.6-flash", "model_api_key": None}
    )
    with TestClient(create_app(configured, mongo), raise_server_exceptions=False) as http:
        created = http.post(
            "/api/vendors",
            json={
                "name": "Model Failure Store",
                "slug": f"model-failure-{uuid4().hex[:8]}",
                "source": {"kind": "csv", "path": str(source)},
                "public": True,
            },
        )
        assert created.status_code == 201
        vendor_id = created.json()["vendor"]["_id"]
        assert http.post(f"/api/vendors/{vendor_id}/sync").status_code == 200
        response = http.post(
            f"/api/vendors/{vendor_id}/chat", json={"message": "Moss Lamp", "history": []}
        )
        problem = _assert_problem(response, 502)
        body = response.text.casefold()
        assert problem["detail"] == settings.commerce.model.unavailable
        assert "live-credential-must-not-appear" not in body
        assert "runtimeerror" not in body
        assert "traceback" not in body
