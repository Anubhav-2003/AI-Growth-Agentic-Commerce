"""Razorpay Test Mode checkout, bounded authorization, and unavailable agentic adapter."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymongo import MongoClient

from config import Settings, get_settings
from main import create_app
from services.payments import (
    CreatedOrder,
    PaymentUnavailable,
    ProviderPayment,
    RazorpayAgenticProvider,
    build_payment_provider,
)

_DATABASE_PREFIX = "commerceos_payments_pytest_"


class FakeCheckout:
    """Stand in for Razorpay Orders/Payments APIs without network or live keys."""

    name = "razorpay_checkout"

    def __init__(self, secret: str = "test_secret") -> None:
        self.secret = secret
        self.orders: list[dict[str, Any]] = []
        self.payments: dict[str, ProviderPayment] = {}
        self.webhook_secret = "whsec"
        self.fetch_failures_remaining = 0

    def available(self) -> bool:
        return True

    def create_payment(
        self, *, amount_minor: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> CreatedOrder:
        order_id = f"order_{uuid4().hex[:14]}"
        self.orders.append(
            {
                "id": order_id,
                "amount": amount_minor,
                "currency": currency,
                "receipt": receipt,
                "notes": notes,
            }
        )
        return CreatedOrder(order_id, amount_minor, currency.upper())

    def verify_signature(self, *, order_id: str, payment_id: str, signature: str) -> None:
        expected = hmac.new(
            self.secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("The payment signature could not be verified.")

    def get_payment_status(self, payment_id: str) -> ProviderPayment:
        if self.fetch_failures_remaining > 0:
            self.fetch_failures_remaining -= 1
            raise PaymentUnavailable(
                "Payment may have succeeded, but we couldn't confirm it with Razorpay yet. "
                "Don't pay again. Retry payment confirmation."
            )
        found = self.payments.get(payment_id)
        if found is None:
            raise ValueError("The payment could not be verified with the provider.")
        return found

    def verify_webhook(self, body: bytes, signature: str) -> None:
        if signature != self.webhook_secret:
            raise ValueError("The webhook could not be verified.")


@pytest.fixture
def payment_environment(
    tmp_path: Path,
) -> tuple[Settings, MongoClient[dict[str, Any]], Path, str, FakeCheckout]:
    """Yield an isolated Mongo database and a fake Standard Checkout provider."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    base = get_settings()
    database_name = f"{_DATABASE_PREFIX}{uuid4().hex}"
    UUID(database_name.removeprefix(_DATABASE_PREFIX))
    mongo: MongoClient[dict[str, Any]] = MongoClient(
        base.mongodb_uri,
        serverSelectionTimeoutMS=base.commerce.limits.mongo_timeout_milliseconds,
    )
    fake = FakeCheckout()
    settings = Settings(
        config_path=base.config_path,
        mongodb_uri=base.mongodb_uri,
        mongodb_database=database_name,
        source_roots=[source_root],
        app_env="test",
        app_host="127.0.0.1",
        app_port=8000,
        admin_api_key=None,
        razorpay_enabled=True,
        razorpay_key_id="rzp_test_public",
        razorpay_key_secret=SecretStr("test_secret"),
        razorpay_webhook_secret=SecretStr("whsec"),
    )
    try:
        mongo.admin.command("ping")
        yield settings, mongo, source_root, database_name, fake
    finally:
        mongo.drop_database(database_name)
        mongo.close()


@pytest.fixture
def payment_client(
    payment_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str, FakeCheckout],
) -> TestClient:
    """Serve the app with Test Mode enabled and a mocked Razorpay client."""
    settings, mongo, _, _, fake = payment_environment
    with TestClient(
        create_app(settings, mongo, payment_provider=fake), raise_server_exceptions=False
    ) as http:
        yield http


def _sign(order_id: str, payment_id: str, secret: str = "test_secret") -> str:
    """Build the Checkout HMAC using the stored-order formula."""
    return hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


def _publish(client: TestClient, source_root: Path) -> tuple[str, dict[str, Any]]:
    """Publish the stocked three-product catalog used by purchase tests."""
    source = source_root / f"shop-{uuid4().hex[:8]}.csv"
    source.write_text(
        "sku,name,description,brand,price,currency,stock_quantity,availability\n"
        "FAS-001,Classic Canvas Sneakers,Everyday sneakers,Northline,59.95,USD,33,in_stock\n"
        "FAS-002,Waterproof Hiking Jacket,Outdoor shell,Trailmark,119.00,USD,9,low_stock\n"
        "SPT-001,Adjustable Yoga Mat,Exercise mat,FlexForm,39.99,USD,27,in_stock\n",
        encoding="utf-8",
    )
    created = client.post(
        "/api/vendors",
        json={
            "name": "Example Store",
            "slug": f"pay-shop-{uuid4().hex[:8]}",
            "source": {"kind": "csv", "path": str(source)},
            "public": True,
        },
    )
    vendor_id = created.json()["vendor"]["_id"]
    assert client.post(f"/api/vendors/{vendor_id}/sync").status_code == 200
    assert (
        client.put(
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
                        "inventory": "stock_quantity",
                    },
                    "price_units": "major",
                }
            },
        ).status_code
        == 200
    )
    records = client.get(f"/api/vendors/{vendor_id}/records", params={"resource": source.stem})
    return vendor_id, {item["data"]["sku"]: item for item in records.json()["items"]}


def _stock(client: TestClient, vendor_id: str, resource: str, sku: str) -> int:
    """Read current mapped inventory for one SKU."""
    records = client.get(f"/api/vendors/{vendor_id}/records", params={"resource": resource})
    item = next(row for row in records.json()["items"] if row["data"]["sku"] == sku)
    return int(item["commerce"]["inventory"])


def test_disabled_razorpay_authorizes_without_starting_payment(tmp_path: Path) -> None:
    """Phase 2 behavior remains when Test Mode is off."""
    base = get_settings()
    database_name = f"{_DATABASE_PREFIX}{uuid4().hex}"
    mongo: MongoClient[dict[str, Any]] = MongoClient(base.mongodb_uri)
    source_root = tmp_path / "off"
    source_root.mkdir()
    settings = Settings(
        config_path=base.config_path,
        mongodb_uri=base.mongodb_uri,
        mongodb_database=database_name,
        source_roots=[source_root],
        app_env="test",
        app_host="127.0.0.1",
        app_port=8000,
        razorpay_enabled=False,
    )
    try:
        with TestClient(create_app(settings, mongo), raise_server_exceptions=False) as client:
            vendor_id, by_sku = _publish(client, source_root)
            review = client.post(
                f"/api/vendors/{vendor_id}/purchases/review",
                json={"items": [{"record_id": by_sku["FAS-001"]["_id"], "quantity": 1}]},
            )
            attempt = review.json()["purchase"]["id"]
            authorized = client.post(
                f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
                json={"confirm": True},
            )
            body = authorized.json()
            assert authorized.status_code == 200
            assert body["purchase"]["status"] == "authorized"
            assert body["purchase"]["payment"]["started"] is False
            assert body["purchase"].get("checkout") in (None, {})
            assert "key_secret" not in authorized.text.casefold()
    finally:
        mongo.drop_database(database_name)
        mongo.close()


def test_missing_credentials_are_a_safe_error(tmp_path: Path) -> None:
    """Enabled Test Mode without keys must not create a provider order."""
    base = get_settings()
    database_name = f"{_DATABASE_PREFIX}{uuid4().hex}"
    mongo: MongoClient[dict[str, Any]] = MongoClient(base.mongodb_uri)
    source_root = tmp_path / "missing"
    source_root.mkdir()
    settings = Settings(
        config_path=base.config_path,
        mongodb_uri=base.mongodb_uri,
        mongodb_database=database_name,
        source_roots=[source_root],
        app_env="test",
        app_host="127.0.0.1",
        app_port=8000,
        razorpay_enabled=True,
        razorpay_key_id=None,
        razorpay_key_secret=None,
    )
    try:
        with TestClient(create_app(settings, mongo), raise_server_exceptions=False) as client:
            vendor_id, by_sku = _publish(client, source_root)
            review = client.post(
                f"/api/vendors/{vendor_id}/purchases/review",
                json={"items": [{"record_id": by_sku["FAS-001"]["_id"], "quantity": 1}]},
            )
            attempt = review.json()["purchase"]["id"]
            response = client.post(
                f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
                json={"confirm": True},
            )
            assert response.status_code == 400
            assert "credentials" in response.json()["detail"].casefold()
            assert "key_secret" not in response.text.casefold()
    finally:
        mongo.drop_database(database_name)
        mongo.close()


def test_checkout_uses_server_amount_and_verifies_captured_payment(
    payment_client: TestClient,
    payment_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str, FakeCheckout],
) -> None:
    """Create a Test order from catalog totals and fulfill only a verified captured payment."""
    _settings, mongo, source_root, database_name, fake = payment_environment
    vendor_id, by_sku = _publish(payment_client, source_root)
    sneakers = by_sku["FAS-001"]
    jacket = by_sku["FAS-002"]
    resource = sneakers["resource"]
    before = _stock(payment_client, vendor_id, resource, "FAS-001")
    review = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={
            "items": [
                {"record_id": sneakers["_id"], "quantity": 2, "displayed_price": 1},
                {"record_id": jacket["_id"], "quantity": 1},
            ]
        },
    )
    attempt = review.json()["purchase"]["id"]
    authorized = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
        json={"confirm": True, "max_amount": 250},
    )
    assert authorized.status_code == 200
    purchase = authorized.json()["purchase"]
    assert purchase["status"] == "payment_pending"
    assert purchase["payment"]["started"] is True
    assert purchase["payment"]["succeeded"] is False
    checkout = purchase["checkout"]
    assert checkout["key_id"] == "rzp_test_public"
    assert checkout["amount"] == 23890
    assert checkout["currency"] == "USD"
    assert fake.orders[0]["amount"] == 23890
    assert "test_secret" not in authorized.text
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == before

    payment_id = "pay_captured_1"
    fake.payments[payment_id] = ProviderPayment(
        payment_id=payment_id,
        order_id=checkout["order_id"],
        amount_minor=23890,
        currency="USD",
        status="captured",
        captured=True,
    )
    verified = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={
            "razorpay_payment_id": payment_id,
            "razorpay_order_id": checkout["order_id"],
            "razorpay_signature": _sign(checkout["order_id"], payment_id),
        },
    )
    assert verified.status_code == 200
    assert verified.json()["purchase"]["status"] == "paid"
    assert "inventory has been updated" in verified.json()["message"].casefold()
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == before - 2
    assert _stock(payment_client, vendor_id, resource, "FAS-002") == 8
    stored = mongo[database_name]["purchase_attempts"].find_one({"attempt_id": attempt})
    assert stored["payment"]["succeeded"] is True
    types = [item["type"] for item in stored["events"]]
    assert "payment_authorized" in types
    assert "payment_started" in types
    assert "payment_succeeded" in types
    assert "key_secret" not in str(stored).casefold()

    duplicate = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={
            "razorpay_payment_id": payment_id,
            "razorpay_order_id": checkout["order_id"],
            "razorpay_signature": _sign(checkout["order_id"], payment_id),
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["purchase"]["status"] == "paid"
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == before - 2


def test_invalid_signature_wrong_order_and_uncaptured_payment_do_not_fulfill(
    payment_client: TestClient,
    payment_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str, FakeCheckout],
) -> None:
    """Rejected callbacks leave catalog stock unchanged."""
    _, _, source_root, _, fake = payment_environment
    vendor_id, by_sku = _publish(payment_client, source_root)
    sneakers = by_sku["FAS-001"]
    resource = sneakers["resource"]
    before = _stock(payment_client, vendor_id, resource, "FAS-001")
    review = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": sneakers["_id"], "quantity": 1}]},
    )
    attempt = review.json()["purchase"]["id"]
    authorized = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
        json={"confirm": True},
    )
    order_id = authorized.json()["purchase"]["checkout"]["order_id"]
    payment_id = "pay_bad"
    fake.payments[payment_id] = ProviderPayment(
        payment_id=payment_id,
        order_id=order_id,
        amount_minor=5995,
        currency="USD",
        status="captured",
        captured=True,
    )
    bad_signature = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={
            "razorpay_payment_id": payment_id,
            "razorpay_order_id": order_id,
            "razorpay_signature": "deadbeef",
        },
    )
    assert bad_signature.status_code == 400
    wrong_order = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={
            "razorpay_payment_id": payment_id,
            "razorpay_order_id": "order_other",
            "razorpay_signature": _sign("order_other", payment_id),
        },
    )
    assert wrong_order.status_code == 400
    fake.payments[payment_id] = ProviderPayment(
        payment_id=payment_id,
        order_id=order_id,
        amount_minor=5995,
        currency="USD",
        status="failed",
        captured=False,
    )
    uncaptured = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={
            "razorpay_payment_id": payment_id,
            "razorpay_order_id": order_id,
            "razorpay_signature": _sign(order_id, payment_id),
        },
    )
    assert uncaptured.status_code == 400
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == before


def test_cancelled_purchase_cannot_be_paid_and_failed_checkout_skips_inventory(
    payment_client: TestClient,
    payment_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str, FakeCheckout],
) -> None:
    """Cancel and Checkout failure never decrement stock."""
    _, _, source_root, _, fake = payment_environment
    vendor_id, by_sku = _publish(payment_client, source_root)
    sneakers = by_sku["FAS-001"]
    resource = sneakers["resource"]
    review = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": sneakers["_id"], "quantity": 1}]},
    )
    attempt = review.json()["purchase"]["id"]
    payment_client.post(f"/api/vendors/{vendor_id}/purchases/{attempt}/cancel")
    authorized = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
        json={"confirm": True},
    )
    assert authorized.status_code == 400
    review2 = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": sneakers["_id"], "quantity": 1}]},
    )
    attempt2 = review2.json()["purchase"]["id"]
    started = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt2}/authorize",
        json={"confirm": True},
    )
    failed = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt2}/payment/failed",
    )
    assert failed.json()["purchase"]["status"] == "failed"
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == 33
    assert started.json()["purchase"]["checkout"]["order_id"]
    assert fake.orders


def test_spending_limit_vendor_and_expiry_are_enforced(
    payment_client: TestClient,
    payment_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str, FakeCheckout],
) -> None:
    """Phase 3B rejects over-limit, expired, and chat-only authorization attempts."""
    _, mongo, source_root, database_name, _fake = payment_environment
    vendor_id, by_sku = _publish(payment_client, source_root)
    sneakers = by_sku["FAS-001"]
    review = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": sneakers["_id"], "quantity": 2}]},
    )
    attempt = review.json()["purchase"]["id"]
    over = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
        json={"confirm": True, "max_amount": 100},
    )
    assert over.status_code == 400
    assert "spending limit" in over.json()["detail"].casefold()
    allowed = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
        json={"confirm": True, "max_amount": 150},
    )
    assert allowed.status_code == 200
    assert allowed.json()["purchase"]["authorization"]["max_amount"] == 150
    chat = payment_client.post(
        f"/api/vendors/{vendor_id}/chat",
        json={"message": "Buy it now and charge my card.", "history": []},
    )
    assert chat.status_code == 200
    purchases = mongo[database_name]["purchase_attempts"]
    assert purchases.count_documents({"status": "paid"}) == 0
    purchases.update_one(
        {"attempt_id": attempt},
        {"$set": {"authorization.expires_at": datetime.now(UTC) - timedelta(seconds=1)}},
    )
    order_id = allowed.json()["purchase"]["checkout"]["order_id"]
    _fake.payments["pay_late"] = ProviderPayment(
        payment_id="pay_late",
        order_id=order_id,
        amount_minor=11990,
        currency="USD",
        status="captured",
        captured=True,
    )
    verify = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={
            "razorpay_payment_id": "pay_late",
            "razorpay_order_id": order_id,
            "razorpay_signature": _sign(order_id, "pay_late"),
        },
    )
    assert verify.status_code == 400


def test_agentic_provider_is_unavailable_and_does_not_fake_a_charge() -> None:
    """Phase 3C stays inert until Razorpay grants UPI Reserve Pay / Agentic access."""
    provider = RazorpayAgenticProvider()
    assert provider.available() is False
    with pytest.raises(PaymentUnavailable, match="UPI Reserve Pay"):
        provider.create_payment(amount_minor=100, currency="INR", receipt="x", notes={})
    disabled = build_payment_provider(
        Settings(
            config_path=get_settings().config_path,
            mongodb_uri="mongodb://unused.invalid",
            mongodb_database="unused",
            source_roots=[Path(".")],
            app_env="test",
            app_host="127.0.0.1",
            app_port=8000,
            razorpay_enabled=False,
        )
    )
    assert disabled.available() is False


def test_captured_webhook_fulfills_once(
    payment_client: TestClient,
    payment_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str, FakeCheckout],
) -> None:
    """A signed payment.captured webhook uses the same fulfillment path as Checkout verify."""
    _, mongo, source_root, database_name, _fake = payment_environment
    vendor_id, by_sku = _publish(payment_client, source_root)
    sneakers = by_sku["FAS-001"]
    resource = sneakers["resource"]
    before = _stock(payment_client, vendor_id, resource, "FAS-001")
    review = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": sneakers["_id"], "quantity": 1}]},
    )
    attempt = review.json()["purchase"]["id"]
    authorized = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
        json={"confirm": True},
    )
    order_id = authorized.json()["purchase"]["checkout"]["order_id"]
    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_hook_1",
                        "order_id": order_id,
                        "amount": 5995,
                        "currency": "USD",
                        "status": "captured",
                    }
                }
            },
        }
    ).encode()
    headers = {"X-Razorpay-Signature": "whsec", "X-Razorpay-Event-Id": "evt_dup"}
    first = payment_client.post("/api/payments/razorpay/webhook", content=body, headers=headers)
    second = payment_client.post("/api/payments/razorpay/webhook", content=body, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == before - 1
    assert mongo[database_name]["purchase_attempts"].find_one({"attempt_id": attempt})[
        "status"
    ] == ("paid")
    bad = payment_client.post(
        "/api/payments/razorpay/webhook",
        content=body,
        headers={"X-Razorpay-Signature": "wrong"},
    )
    assert bad.status_code == 400


def _captured(fake: FakeCheckout, order_id: str, payment_id: str, amount: int = 5995) -> None:
    """Register one captured TEST payment on the fake provider."""
    fake.payments[payment_id] = ProviderPayment(
        payment_id=payment_id,
        order_id=order_id,
        amount_minor=amount,
        currency="USD",
        status="captured",
        captured=True,
    )


def test_temporary_fetch_failure_is_retryable_on_the_same_attempt(
    payment_client: TestClient,
    payment_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str, FakeCheckout],
) -> None:
    """A provider fetch outage keeps verification pending and retains the payment id."""
    _, mongo, source_root, database_name, fake = payment_environment
    vendor_id, by_sku = _publish(payment_client, source_root)
    sneakers = by_sku["FAS-001"]
    resource = sneakers["resource"]
    before = _stock(payment_client, vendor_id, resource, "FAS-001")
    review = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": sneakers["_id"], "quantity": 1}]},
    )
    attempt = review.json()["purchase"]["id"]
    authorized = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
        json={"confirm": True},
    )
    order_id = authorized.json()["purchase"]["checkout"]["order_id"]
    _captured(fake, order_id, "pay_retry")
    fake.fetch_failures_remaining = 1
    first = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={
            "razorpay_payment_id": "pay_retry",
            "razorpay_order_id": order_id,
            "razorpay_signature": _sign(order_id, "pay_retry"),
        },
    )
    assert first.status_code == 200
    body = first.json()
    assert body["retryable"] is True
    assert "don't pay again" in body["message"].casefold()
    purchase = body["purchase"]
    assert purchase["status"] == "payment_pending"
    assert purchase["payment"]["status"] == "verification_pending"
    assert purchase["payment"]["succeeded"] is False
    stored = mongo[database_name]["purchase_attempts"].find_one({"attempt_id": attempt})
    assert stored["payment"]["razorpay_payment_id"] == "pay_retry"
    assert stored["payment"]["status"] == "verification_pending"
    assert "payment_verification_unavailable" in [item["type"] for item in stored["events"]]
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == before
    assert mongo[database_name]["catalog_orders"].count_documents({"attempt_id": attempt}) == 0
    assert "key_secret" not in first.text.casefold()

    blocked = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
        json={"confirm": True},
    )
    assert blocked.status_code == 400
    assert "do not pay again" in blocked.json()["detail"].casefold()
    assert len(fake.orders) == 1

    retry = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={},
    )
    assert retry.status_code == 200
    assert retry.json()["purchase"]["status"] == "paid"
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == before - 1
    assert mongo[database_name]["catalog_orders"].count_documents({"attempt_id": attempt}) == 1
    duplicate = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["purchase"]["status"] == "paid"
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == before - 1
    assert mongo[database_name]["catalog_orders"].count_documents({"attempt_id": attempt}) == 1


def test_amount_mismatch_never_fulfills(
    payment_client: TestClient,
    payment_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str, FakeCheckout],
) -> None:
    """A captured payment for the wrong amount cannot decrement inventory."""
    _, _, source_root, _, fake = payment_environment
    vendor_id, by_sku = _publish(payment_client, source_root)
    sneakers = by_sku["FAS-001"]
    resource = sneakers["resource"]
    before = _stock(payment_client, vendor_id, resource, "FAS-001")
    review = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": sneakers["_id"], "quantity": 1}]},
    )
    attempt = review.json()["purchase"]["id"]
    authorized = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
        json={"confirm": True},
    )
    order_id = authorized.json()["purchase"]["checkout"]["order_id"]
    _captured(fake, order_id, "pay_wrong_amount", amount=1)
    response = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={
            "razorpay_payment_id": "pay_wrong_amount",
            "razorpay_order_id": order_id,
            "razorpay_signature": _sign(order_id, "pay_wrong_amount"),
        },
    )
    assert response.status_code == 400
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == before


def test_webhook_fulfills_verification_pending_once(
    payment_client: TestClient,
    payment_environment: tuple[Settings, MongoClient[dict[str, Any]], Path, str, FakeCheckout],
) -> None:
    """A captured webhook recovers a pending verification without a second decrement."""
    _, mongo, source_root, database_name, fake = payment_environment
    vendor_id, by_sku = _publish(payment_client, source_root)
    sneakers = by_sku["FAS-001"]
    resource = sneakers["resource"]
    before = _stock(payment_client, vendor_id, resource, "FAS-001")
    review = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/review",
        json={"items": [{"record_id": sneakers["_id"], "quantity": 1}]},
    )
    attempt = review.json()["purchase"]["id"]
    authorized = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/authorize",
        json={"confirm": True},
    )
    order_id = authorized.json()["purchase"]["checkout"]["order_id"]
    _captured(fake, order_id, "pay_hook_pending")
    fake.fetch_failures_remaining = 1
    pending = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={
            "razorpay_payment_id": "pay_hook_pending",
            "razorpay_order_id": order_id,
            "razorpay_signature": _sign(order_id, "pay_hook_pending"),
        },
    )
    assert pending.json()["purchase"]["payment"]["status"] == "verification_pending"
    body = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_hook_pending",
                        "order_id": order_id,
                        "amount": 5995,
                        "currency": "USD",
                        "status": "captured",
                    }
                }
            },
        }
    ).encode()
    headers = {"X-Razorpay-Signature": "whsec", "X-Razorpay-Event-Id": "evt_pending"}
    first = payment_client.post("/api/payments/razorpay/webhook", content=body, headers=headers)
    second = payment_client.post("/api/payments/razorpay/webhook", content=body, headers=headers)
    verify = payment_client.post(
        f"/api/vendors/{vendor_id}/purchases/{attempt}/payment/verify",
        json={},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert verify.status_code == 200
    assert verify.json()["purchase"]["status"] == "paid"
    assert _stock(payment_client, vendor_id, resource, "FAS-001") == before - 1
    assert mongo[database_name]["catalog_orders"].count_documents({"attempt_id": attempt}) == 1
