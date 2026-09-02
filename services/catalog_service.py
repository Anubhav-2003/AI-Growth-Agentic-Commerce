from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from bson import ObjectId
from gridfs import GridFSBucket
from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel, ReplaceOne, UpdateOne
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError
from slugify import slugify

from config import CommerceConfig
from services.payments import PaymentUnavailable

Document = dict[str, Any]
VendorReference = str | ObjectId
_CURSOR = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")
_MINOR_PER_MAJOR = Decimal("100")


def _payload(value: Any) -> Document:
    """Return an explicit payload from Pydantic models or ordinary mappings."""
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_unset=True)
    return dict(value)


def _public(value: Any) -> Any:
    """Convert BSON identifiers recursively while leaving source values untouched."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _path_value(data: Mapping[str, Any], path: str) -> Any:
    """Read an explicit field path, preferring an exact key containing dots."""
    if path in data:
        return data[path]
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _minor_price(value: Any, units: str | None) -> int | None:
    """Convert an explicitly unit-labelled finite price without rounding it."""
    if isinstance(value, Mapping) and value.get("$commerceos_type") in {
        "decimal",
        "integer",
    }:
        value = value.get("value")
    if isinstance(value, bool) or units not in {"major", "minor"}:
        return None
    try:
        amount = Decimal(str(value)) * (_MINOR_PER_MAJOR if units == "major" else 1)
    except (InvalidOperation, ValueError):
        return None
    return (
        int(amount)
        if amount.is_finite() and amount >= 0 and amount == amount.to_integral()
        else None
    )


def _count(value: Any) -> int | None:
    """Read a non-negative whole stock count without mutating catalog inventory."""
    if isinstance(value, Mapping) and value.get("$commerceos_type") in {
        "decimal",
        "integer",
        "float",
    }:
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if amount.is_finite() and amount >= 0 and amount == amount.to_integral_value():
        return int(amount)
    return None


def _major_units(minor: int) -> int | float:
    """Convert stored minor units into a shopper-facing major amount."""
    amount = Decimal(minor) / _MINOR_PER_MAJOR
    return int(amount) if amount == amount.to_integral() else float(amount)


def _to_minor(value: Any) -> int:
    """Convert a shopper-facing major amount into catalog minor units."""
    try:
        amount = Decimal(str(value)) * _MINOR_PER_MAJOR
    except (InvalidOperation, ValueError) as error:
        raise ValueError("The authorized amount is not valid.") from error
    if amount.is_finite() and amount >= 0 and amount == amount.to_integral():
        return int(amount)
    raise ValueError("The authorized amount is not valid.")


class CatalogService:
    """Keep Mongo persistence, catalog queries, and deterministic projections together."""

    def __init__(self, database: Database[Document], config: CommerceConfig) -> None:
        """Bind configured collections without opening or mutating merchant sources."""
        self.database, self.config = database, config
        names = config.collections
        self.vendors = database[names.vendors]
        self.resources = database[names.resources]
        self.records = database[names.records]
        self.syncs = database[names.syncs]
        self.purchases = database[names.purchases]
        self.orders = database[names.orders]
        self.artifact_bucket_name = f"{names.syncs}_artifacts"
        self.artifacts = GridFSBucket(database, bucket_name=self.artifact_bucket_name)

    def ensure_indexes(self) -> None:
        """Create the small set of indexes required by reads and revision publication."""
        self.vendors.create_indexes(
            [
                IndexModel("slug", unique=True, sparse=True),
                IndexModel([("updated_at", DESCENDING)]),
            ]
        )
        self.resources.create_indexes(
            [
                IndexModel(
                    [("vendor_id", ASCENDING), ("sync_id", ASCENDING), ("name", ASCENDING)],
                    unique=True,
                )
            ]
        )
        self.records.create_indexes(
            [
                IndexModel(
                    [
                        ("vendor_id", ASCENDING),
                        ("sync_id", ASCENDING),
                        ("resource", ASCENDING),
                        ("record_id", ASCENDING),
                    ],
                    unique=True,
                ),
                IndexModel(
                    [("vendor_id", ASCENDING), ("sync_id", ASCENDING), ("commerce.id", ASCENDING)]
                ),
                IndexModel([("search_text", TEXT)]),
            ]
        )
        self.syncs.create_indexes(
            [
                IndexModel("sync_id", unique=True),
                IndexModel([("vendor_id", ASCENDING), ("started_at", DESCENDING)]),
            ]
        )
        self.purchases.create_indexes(
            [
                IndexModel("attempt_id", unique=True),
                IndexModel([("vendor_id", ASCENDING), ("created_at", DESCENDING)]),
                IndexModel("payment.razorpay_order_id", sparse=True),
                IndexModel("payment.razorpay_payment_id", sparse=True),
            ]
        )
        self.orders.create_indexes(
            [
                IndexModel("order_id", unique=True),
                IndexModel("attempt_id", unique=True),
            ]
        )
        self.database[f"{self.artifact_bucket_name}.files"].create_index(
            [("metadata.vendor_id", ASCENDING), ("metadata.digest", ASCENDING)],
            unique=True,
            partialFilterExpression={
                "metadata.vendor_id": {"$exists": True},
                "metadata.digest": {"$exists": True},
            },
        )

    def create_vendor(self, payload: Any) -> Document:
        """Create a current-shape vendor while accepting the legacy registration payload."""
        data, now = _payload(payload), datetime.now(UTC)
        source = self._source(data)
        name = str(data.get("name", "")).strip()
        vendor_slug = str(data.get("slug") or slugify(name)).strip()
        if not name or not vendor_slug or not source.get("kind") or not source.get("path"):
            raise ValueError("Vendor name, slug, source kind, and source path are required.")
        document = {
            **data,
            "name": name,
            "slug": vendor_slug,
            "source": source,
            "public": bool(data.get("public", True)),
            "status": "registered",
            "created_at": now,
            "updated_at": now,
        }
        document.pop("_id", None)
        result = self.vendors.insert_one(document)
        return self.get_vendor(result.inserted_id) or {}

    def list_vendors(self, public: bool | None = None) -> list[Document]:
        """List vendors without rewriting legacy documents as a side effect."""
        query: Document = {}
        if public is True:
            query = {"$or": [{"public": True}, {"public": {"$exists": False}}]}
        elif public is False:
            query = {"public": False}
        return [
            self._vendor_view(item) for item in self.vendors.find(query).sort("name", ASCENDING)
        ]

    def get_vendor(self, reference: VendorReference) -> Document | None:
        """Resolve a vendor by stored identifier or slug, including legacy records."""
        document = self._find_vendor(reference)
        return self._vendor_view(document) if document else None

    def get_vendor_by_slug(self, slug: str, public_only: bool = False) -> Document | None:
        """Resolve a public store slug and fall back to generated legacy slugs."""
        access = {"$or": [{"public": True}, {"public": {"$exists": False}}]} if public_only else {}
        document = self.vendors.find_one({"slug": slug, **access})
        if document:
            return self._vendor_view(document)
        legacy = self.vendors.find({"slug": {"$exists": False}, **access})
        return next(
            (self._vendor_view(item) for item in legacy if self._vendor_view(item)["slug"] == slug),
            None,
        )

    def update_vendor(self, reference: VendorReference, changes: Any) -> Document | None:
        """Apply validated fields with `$set` so unknown legacy fields are preserved."""
        vendor = self._find_vendor(reference)
        if not vendor:
            return None
        updates = _payload(changes)
        updates.pop("_id", None)
        if any(key in updates for key in ("source", "db_path", "location", "format", "type")):
            updates["source"] = self._source({**vendor, **updates})
        updates["updated_at"] = datetime.now(UTC)
        self.vendors.update_one({"_id": vendor["_id"]}, {"$set": updates})
        return self.get_vendor(vendor["_id"])

    def update_mapping(self, reference: VendorReference, mapping: Any) -> Document | None:
        """Store one explicit mapping and refresh only its additive commerce projection."""
        value = _payload(mapping)
        value = _payload(value["mapping"]) if "mapping" in value else value
        vendor = self.update_vendor(reference, {"mapping": value})
        if vendor and vendor.get("active_sync_id"):
            self.project_products(vendor["_id"], value)
        return self.get_vendor(vendor["_id"]) if vendor else None

    def delete_vendor(self, reference: VendorReference) -> bool:
        """Delete exactly one vendor and its derived catalog/artifacts when explicitly called."""
        vendor = self._find_vendor(reference)
        if not vendor:
            return False
        key = vendor["_id"]
        for file in self.database[f"{self.artifact_bucket_name}.files"].find(
            {"metadata.vendor_id": key}
        ):
            self.artifacts.delete(file["_id"])
        for collection in (self.resources, self.records, self.syncs, self.purchases, self.orders):
            collection.delete_many({"vendor_id": key})
        return self.vendors.delete_one({"_id": key}).deleted_count == 1

    def list_resources(
        self, reference: VendorReference, sync_id: str | None = None
    ) -> list[Document]:
        """Return resource metadata from the requested or currently published revision."""
        vendor = self._find_vendor(reference)
        revision = sync_id or (vendor or {}).get("active_sync_id")
        if not vendor or not revision:
            return []
        cursor = self.resources.find({"vendor_id": vendor["_id"], "sync_id": revision}).sort(
            "name", ASCENDING
        )
        return [_public(item) for item in cursor]

    def get_resource(
        self, reference: VendorReference, name: str, sync_id: str | None = None
    ) -> Document | None:
        """Return one resource from an explicit or active revision."""
        vendor = self._find_vendor(reference)
        revision = sync_id or (vendor or {}).get("active_sync_id")
        if not vendor or not revision:
            return None
        document = self.resources.find_one(
            {"vendor_id": vendor["_id"], "sync_id": revision, "name": name}
        )
        return _public(document) if document else None

    def list_records(
        self,
        reference: VendorReference,
        resource: str | None = None,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        query: str | None = None,
        commerce_only: bool = False,
    ) -> Document:
        """Page active records in stable-ID order with an optional literal text query."""
        return self._record_page(reference, resource, cursor, limit, query, commerce_only)

    def search_records(
        self,
        reference: VendorReference,
        query: str,
        *,
        resource: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        commerce_only: bool = False,
    ) -> Document:
        """Search active scalar text while preserving the normal cursor contract."""
        if not query.strip():
            raise ValueError("Search query cannot be empty.")
        return self._record_page(reference, resource, cursor, limit, query, commerce_only)

    def get_record(self, reference: VendorReference, record_id: str) -> Document | None:
        """Resolve an active record by stable normalized ID or exact mapped product ID."""
        vendor = self._find_vendor(reference)
        revision = (vendor or {}).get("active_sync_id")
        if not vendor or not revision:
            return None
        query = {
            "vendor_id": vendor["_id"],
            "sync_id": revision,
            "$or": [{"record_id": record_id}, {"commerce.id": record_id}],
        }
        document = self.records.find_one(query)
        return self._record_view(document) if document else None

    def review_purchase(self, reference: VendorReference, items: Any) -> Document:
        """Validate current catalog offers and record a review without starting payment."""
        return self._store_purchase(reference, items, status="review")

    def authorize_purchase(
        self,
        reference: VendorReference,
        attempt_id: str,
        confirm: bool,
        max_amount: float | None = None,
    ) -> Document:
        """Record explicit bounded consent after re-validating catalog facts."""
        if confirm is not True:
            self._append_event(reference, attempt_id, "payment_authorization_rejected")
            raise ValueError("Explicit purchase confirmation is required before payment.")
        vendor, document = self._purchase_document(reference, attempt_id)
        if document["status"] in {"cancelled", "failed", "paid"}:
            raise ValueError("This purchase cannot be authorized.")
        if document["status"] not in {"review", "authorized", "payment_pending"}:
            raise ValueError("This purchase cannot be authorized.")
        existing_payment = document.get("payment") or {}
        if (
            document["status"] == "payment_pending"
            and str(existing_payment.get("razorpay_order_id") or "")
            and existing_payment.get("status") in {"pending", "verification_pending"}
        ):
            raise ValueError(
                "Payment confirmation is still pending for this purchase. "
                "Do not pay again. Retry payment confirmation."
            )
        self._append_event(reference, attempt_id, "payment_authorization_requested")
        lines = self._revalidate_attempt_lines(vendor, document)
        total_minor = sum(item["subtotal_minor"] for item in lines)
        currency = lines[0]["currency"]
        ceiling = total_minor if max_amount is None else _to_minor(max_amount)
        if ceiling < total_minor:
            self._append_event(reference, attempt_id, "payment_authorization_rejected")
            raise ValueError(
                "This purchase exceeds the authorized spending limit and cannot continue."
            )
        now = datetime.now(UTC)
        ttl = int(self.config.limits.authorization_ttl_seconds)
        authorization = {
            "authorized": True,
            "max_amount_minor": ceiling,
            "currency": currency,
            "vendor_id": str(vendor["_id"]),
            "purchase_attempt_id": document["attempt_id"],
            "authorized_at": now,
            "expires_at": now + timedelta(seconds=ttl),
            "status": "active",
        }
        updates = {
            "status": "authorized",
            "items": lines,
            "total_minor": total_minor,
            "currency": currency,
            "authorized_at": document.get("authorized_at") or now,
            "updated_at": now,
            "authorization": authorization,
            "payment": {**self._payment_state(), "status": "not_started"},
        }
        self.purchases.update_one({"_id": document["_id"]}, {"$set": updates})
        self._append_event(reference, attempt_id, "payment_authorized")
        return self._purchase_view({**document, **updates})

    def cancel_purchase(self, reference: VendorReference, attempt_id: str) -> Document:
        """Close a purchase attempt without payment or inventory mutation."""
        _, document = self._purchase_document(reference, attempt_id)
        if document["status"] == "cancelled":
            return self._purchase_view(document)
        if document["status"] == "paid" or document.get("fulfillment") == "complete":
            raise ValueError("A paid purchase cannot be cancelled.")
        now = datetime.now(UTC)
        updates = {
            "status": "cancelled",
            "cancelled_at": now,
            "updated_at": now,
            "payment": {
                **dict(document.get("payment") or self._payment_state()),
                "started": bool((document.get("payment") or {}).get("started")),
                "succeeded": False,
                "failed": True,
                "status": "failed",
            },
        }
        self.purchases.update_one({"_id": document["_id"]}, {"$set": updates})
        self._append_event(reference, attempt_id, "purchase_cancelled")
        return self._purchase_view({**document, **updates})

    def start_provider_payment(
        self,
        reference: VendorReference,
        attempt_id: str,
        order: Mapping[str, Any],
        provider_name: str,
    ) -> Document:
        """Attach a provider order after authorization; inventory still does not change."""
        vendor, document = self._purchase_document(reference, attempt_id)
        self._assert_authorization(vendor, document)
        if document["status"] in {"cancelled", "failed", "paid"}:
            raise ValueError("This purchase cannot continue to payment.")
        existing_payment = document.get("payment") or {}
        if str(existing_payment.get("razorpay_order_id") or "") and existing_payment.get(
            "status"
        ) in {"pending", "verification_pending"}:
            raise ValueError(
                "Payment confirmation is still pending for this purchase. "
                "Do not pay again. Retry payment confirmation."
            )
        now = datetime.now(UTC)
        payment = {
            **self._payment_state(),
            "started": True,
            "status": "pending",
            "provider": provider_name,
            "razorpay_order_id": str(order["provider_order_id"]),
            "amount_minor": int(order["amount_minor"]),
            "currency": str(order["currency"]).upper(),
        }
        updates = {
            "status": "payment_pending",
            "updated_at": now,
            "payment": payment,
        }
        self.purchases.update_one({"_id": document["_id"]}, {"$set": updates})
        self._append_event(reference, attempt_id, "payment_started")
        return self._purchase_view({**document, **updates})

    def verify_and_fulfill_payment(
        self,
        reference: VendorReference,
        attempt_id: str,
        *,
        browser_order_id: str | None,
        payment_id: str | None,
        signature: str | None,
        provider: Any,
    ) -> Document:
        """Verify signature and provider status, then run the single fulfillment path."""
        vendor, document = self._purchase_document(reference, attempt_id)
        if document.get("fulfillment") == "complete" or document.get("status") == "paid":
            return self._purchase_view(document)
        stored_order = str((document.get("payment") or {}).get("razorpay_order_id") or "")
        stored_payment = str((document.get("payment") or {}).get("razorpay_payment_id") or "")
        stored_signature = str((document.get("payment") or {}).get("razorpay_signature") or "")
        if document["status"] == "cancelled":
            raise ValueError("This purchase was cancelled and cannot be paid.")
        if not stored_order:
            raise ValueError("Payment has not started for this purchase.")
        browser_order = str(browser_order_id or "").strip()
        if browser_order and browser_order != stored_order:
            self._mark_verification_failed(document)
            self._append_event(reference, attempt_id, "payment_failed")
            raise ValueError("The payment could not be verified.")
        resolved_payment = str(payment_id or "").strip() or stored_payment
        resolved_signature = str(signature or "").strip() or stored_signature
        if (
            str(payment_id or "").strip()
            and stored_payment
            and str(payment_id).strip() != stored_payment
        ):
            self._mark_verification_failed(document)
            self._append_event(reference, attempt_id, "payment_failed")
            raise ValueError("The payment could not be verified.")
        if not resolved_payment or not resolved_signature:
            raise ValueError("The payment could not be verified.")
        try:
            provider.verify_signature(
                order_id=stored_order, payment_id=resolved_payment, signature=resolved_signature
            )
        except ValueError:
            self._mark_verification_failed(document)
            self._append_event(reference, attempt_id, "payment_failed")
            raise
        self._retain_payment_callback(document, resolved_payment, resolved_signature)
        try:
            live = provider.get_payment_status(resolved_payment)
        except PaymentUnavailable:
            self._mark_verification_pending(document, resolved_payment, resolved_signature)
            self._append_event(
                reference,
                attempt_id,
                "payment_verification_unavailable",
                {
                    "attempt_id": attempt_id,
                    "provider": (document.get("payment") or {}).get("provider")
                    or getattr(provider, "name", None),
                    "order_id": stored_order,
                    "error_category": "provider_fetch",
                },
            )
            _, pending = self._purchase_document(reference, attempt_id)
            return self._purchase_view(pending)
        except ValueError:
            self._mark_verification_failed(document)
            self._append_event(reference, attempt_id, "payment_failed")
            raise
        return self.finalize_successful_payment(
            vendor,
            document,
            payment_id=live.payment_id,
            order_id=stored_order,
            amount_minor=live.amount_minor,
            currency=live.currency,
            captured=live.captured,
            live_order_id=live.order_id,
            live_status=live.status,
        )

    def finalize_successful_payment(
        self,
        vendor: Document,
        document: Document,
        *,
        payment_id: str,
        order_id: str,
        amount_minor: int,
        currency: str,
        captured: bool,
        live_order_id: str,
        live_status: str,
    ) -> Document:
        """Idempotently fulfill one verified captured payment and decrement inventory once."""
        if document.get("fulfillment") == "complete" or document.get("status") == "paid":
            stored_payment = str((document.get("payment") or {}).get("razorpay_payment_id") or "")
            if stored_payment in {"", payment_id}:
                return self._purchase_view(document)
            raise ValueError("This purchase has already been fulfilled.")
        if document["status"] == "cancelled":
            raise ValueError("This purchase was cancelled and cannot be paid.")
        stored_order = str((document.get("payment") or {}).get("razorpay_order_id") or "")
        if live_order_id and live_order_id != stored_order:
            self._mark_verification_failed(document)
            raise ValueError("The payment could not be verified.")
        if not captured and live_status != "captured":
            self._mark_verification_failed(document)
            self._append_event(vendor["_id"], document["attempt_id"], "payment_failed")
            raise ValueError("Payment was not completed. No inventory was changed.")
        expected_amount = int(document["total_minor"])
        expected_currency = str(document["currency"]).upper()
        if int(amount_minor) != expected_amount or str(currency).upper() != expected_currency:
            self._mark_verification_failed(document)
            raise ValueError("The payment could not be verified.")
        self._assert_authorization(vendor, document, amount_minor=expected_amount)
        lines = self._revalidate_attempt_lines(vendor, document)
        now = datetime.now(UTC)
        claimed = self.purchases.find_one_and_update(
            {
                "_id": document["_id"],
                "status": {"$in": ["payment_pending", "authorized"]},
                "fulfillment": {"$ne": "complete"},
            },
            {
                "$set": {
                    "fulfillment": "in_progress",
                    "updated_at": now,
                    "payment.razorpay_payment_id": payment_id,
                }
            },
        )
        if claimed is None:
            current = self.purchases.find_one({"_id": document["_id"]}) or document
            if current.get("fulfillment") == "complete" or current.get("status") == "paid":
                return self._purchase_view(current)
            raise ValueError("This purchase cannot be fulfilled.")
        try:
            self._decrement_inventory(vendor, lines)
        except Exception:
            self.purchases.update_one(
                {"_id": document["_id"]},
                {"$set": {"fulfillment": None, "updated_at": datetime.now(UTC)}},
            )
            raise
        order_id_internal = uuid4().hex
        paid_payment = {
            **self._payment_state(),
            "started": True,
            "succeeded": True,
            "failed": False,
            "status": "succeeded",
            "provider": (document.get("payment") or {}).get("provider") or "razorpay_checkout",
            "razorpay_order_id": stored_order,
            "razorpay_payment_id": payment_id,
            "amount_minor": expected_amount,
            "currency": expected_currency,
        }
        updates = {
            "status": "paid",
            "fulfillment": "complete",
            "fulfilled_at": now,
            "updated_at": now,
            "items": lines,
            "payment": paid_payment,
            "order_id": order_id_internal,
        }
        self.purchases.update_one({"_id": document["_id"]}, {"$set": updates})
        with suppress(DuplicateKeyError):
            self.orders.insert_one(
                {
                    "order_id": order_id_internal,
                    "vendor_id": vendor["_id"],
                    "attempt_id": document["attempt_id"],
                    "items": [
                        {
                            "record_id": item["record_id"],
                            "name": item["name"],
                            "quantity": item["quantity"],
                            "unit_price_minor": item["unit_price_minor"],
                            "subtotal_minor": item["subtotal_minor"],
                            "currency": item["currency"],
                        }
                        for item in lines
                    ],
                    "total_minor": expected_amount,
                    "currency": expected_currency,
                    "provider": paid_payment["provider"],
                    "razorpay_order_id": stored_order,
                    "razorpay_payment_id": payment_id,
                    "status": "paid",
                    "created_at": now,
                }
            )
        self._append_event(vendor["_id"], document["attempt_id"], "payment_succeeded")
        return self._purchase_view({**document, **updates})

    def find_purchase_by_provider_order(self, order_id: str) -> tuple[Document, Document] | None:
        """Resolve a purchase attempt from a stored Razorpay order id."""
        document = self.purchases.find_one({"payment.razorpay_order_id": str(order_id or "")})
        if not document:
            return None
        vendor = self._find_vendor(document["vendor_id"])
        if not vendor:
            return None
        return vendor, document

    def mark_payment_failed(self, reference: VendorReference, attempt_id: str) -> Document:
        """Record a failed or abandoned payment without touching inventory."""
        _, document = self._purchase_document(reference, attempt_id)
        if document["status"] in {"paid", "cancelled"}:
            return self._purchase_view(document)
        now = datetime.now(UTC)
        payment = {
            **dict(document.get("payment") or self._payment_state()),
            "succeeded": False,
            "failed": True,
            "status": "failed",
        }
        updates = {"status": "failed", "updated_at": now, "payment": payment}
        self.purchases.update_one({"_id": document["_id"]}, {"$set": updates})
        self._append_event(reference, attempt_id, "payment_failed")
        return self._purchase_view({**document, **updates})

    def _revalidate_attempt_lines(
        self, vendor: Document, document: Mapping[str, Any]
    ) -> list[Document]:
        """Rebuild attempt lines from live catalog identity, price, and stock."""
        lines = self._validated_lines(
            vendor,
            [
                {"record_id": item["record_id"], "quantity": item["quantity"]}
                for item in document.get("items") or []
            ],
        )
        previous = {item["record_id"]: item for item in document.get("items") or []}
        for line in lines:
            shown = previous.get(line["record_id"], {}).get("displayed_price_minor")
            if shown is not None:
                line["displayed_price_minor"] = shown
        return lines

    def _assert_authorization(
        self, vendor: Document, document: Mapping[str, Any], amount_minor: int | None = None
    ) -> None:
        """Enforce vendor, attempt, currency, ceiling, and expiry before money movement."""
        policy = document.get("authorization") or {}
        if not policy.get("authorized") or policy.get("status") != "active":
            raise ValueError("This purchase is not authorized for payment.")
        if str(policy.get("vendor_id")) != str(vendor["_id"]):
            raise ValueError("This purchase is not authorized for payment.")
        if str(policy.get("purchase_attempt_id")) != str(document.get("attempt_id")):
            raise ValueError("This purchase is not authorized for payment.")
        expires = policy.get("expires_at")
        if expires is not None:
            expiry = expires if getattr(expires, "tzinfo", None) else expires.replace(tzinfo=UTC)
            if datetime.now(UTC) > expiry:
                raise ValueError("The payment authorization has expired.")
        total = int(amount_minor if amount_minor is not None else document["total_minor"])
        if total > int(policy.get("max_amount_minor") or 0):
            raise ValueError(
                "This purchase exceeds the authorized spending limit and cannot continue."
            )
        if str(policy.get("currency") or "").upper() != str(document.get("currency") or "").upper():
            raise ValueError("This purchase is not authorized for payment.")

    def _decrement_inventory(self, vendor: Document, lines: list[Document]) -> None:
        """Decrement every line only when each current stock count still covers the quantity."""
        snapshots: list[tuple[Document, Document, int, str | None]] = []
        for line in lines:
            record = self._active_record(vendor, line["record_id"])
            if record is None:
                raise ValueError("A selected product is no longer available.")
            available = self._offer_from_record(vendor, record).get("inventory")
            if available is None:
                raise ValueError("Inventory cannot be updated because stock is not available.")
            if available < int(line["quantity"]):
                raise ValueError(
                    f"Only {available} {line['name']} are currently available. "
                    "Please update your quantity."
                )
            source_key = ((vendor.get("mapping") or {}).get("fields") or {}).get("inventory")
            snapshots.append((record, line, int(available), source_key if source_key else None))
        applied: list[tuple[Document, int, str | None]] = []
        try:
            for record, line, available, source_key in snapshots:
                remaining = available - int(line["quantity"])
                if remaining < 0:
                    raise ValueError("Inventory cannot become negative.")
                updates: Document = {"commerce.inventory": remaining}
                if source_key:
                    current = (record.get("data") or {}).get(source_key)
                    updates[f"data.{source_key}"] = (
                        str(remaining) if isinstance(current, str) else remaining
                    )
                result = self.records.find_one_and_update(
                    {"_id": record["_id"]},
                    {"$set": updates},
                    return_document=False,
                )
                if result is None:
                    raise ValueError("Inventory could not be updated.")
                applied.append((record, available, source_key))
        except Exception:
            for record, previous, source_key in reversed(applied):
                restore: Document = {"commerce.inventory": previous}
                if source_key:
                    current = (record.get("data") or {}).get(source_key)
                    restore[f"data.{source_key}"] = (
                        str(previous) if isinstance(current, str) else previous
                    )
                self.records.update_one({"_id": record["_id"]}, {"$set": restore})
            raise

    def _append_event(
        self,
        reference: VendorReference,
        attempt_id: str,
        name: str,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Store a safe purchase lifecycle event without secrets or payment credentials."""
        try:
            _, document = self._purchase_document(reference, attempt_id)
        except FileNotFoundError:
            return
        event: Document = {"type": name, "at": datetime.now(UTC)}
        if meta:
            event["meta"] = {
                key: value
                for key, value in meta.items()
                if value not in {None, ""} and "secret" not in str(key).casefold()
            }
        self.purchases.update_one({"_id": document["_id"]}, {"$push": {"events": event}})

    def _retain_payment_callback(self, document: Document, payment_id: str, signature: str) -> None:
        """Keep Checkout identifiers after HMAC so a later retry can use the same payment."""
        payment = dict(document.get("payment") or self._payment_state())
        payment["razorpay_payment_id"] = payment_id
        payment["razorpay_signature"] = signature
        self.purchases.update_one(
            {"_id": document["_id"], "status": {"$ne": "paid"}},
            {"$set": {"payment": payment, "updated_at": datetime.now(UTC)}},
        )
        document["payment"] = payment

    def _mark_verification_pending(
        self, document: Document, payment_id: str, signature: str
    ) -> None:
        """Keep the attempt payable after a temporary provider fetch failure."""
        payment = dict(document.get("payment") or self._payment_state())
        payment.update(
            {
                "started": True,
                "succeeded": False,
                "failed": False,
                "status": "verification_pending",
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            }
        )
        self.purchases.update_one(
            {"_id": document["_id"], "status": "payment_pending"},
            {"$set": {"payment": payment, "updated_at": datetime.now(UTC)}},
        )
        document["payment"] = payment

    def _mark_verification_failed(self, document: Mapping[str, Any]) -> None:
        """Keep inventory unchanged when a callback cannot be trusted."""
        payment = {
            **dict(document.get("payment") or self._payment_state()),
            "succeeded": False,
            "failed": True,
            "status": "verification_failed",
        }
        self.purchases.update_one(
            {"_id": document["_id"], "status": {"$ne": "paid"}},
            {"$set": {"payment": payment, "updated_at": datetime.now(UTC)}},
        )

    def _store_purchase(self, reference: VendorReference, items: Any, status: str) -> Document:
        """Persist one auditable purchase attempt after live catalog validation."""
        vendor = self._find_vendor(reference)
        if not vendor or not vendor.get("active_sync_id"):
            raise FileNotFoundError("The requested storefront was not found.")
        lines = self._validated_lines(vendor, items)
        now = datetime.now(UTC)
        document = {
            "attempt_id": uuid4().hex,
            "vendor_id": vendor["_id"],
            "status": status,
            "items": lines,
            "total_minor": sum(item["subtotal_minor"] for item in lines),
            "currency": lines[0]["currency"],
            "created_at": now,
            "updated_at": now,
            "authorized_at": None,
            "cancelled_at": None,
            "payment": self._payment_state(),
        }
        self.purchases.insert_one(document)
        self._append_event(reference, document["attempt_id"], "purchase_reviewed")
        return self._purchase_view(document)

    def _validated_lines(self, vendor: Document, items: Any) -> list[Document]:
        """Rebuild every line from canonical record IDs and current catalog facts."""
        requested = [_payload(item) for item in items]
        if not requested:
            raise ValueError("Select at least one product before continuing to purchase.")
        seen: set[str] = set()
        lines: list[Document] = []
        currency = None
        for item in requested:
            record_id = str(item.get("record_id") or "").strip()
            quantity = int(item.get("quantity") or 0)
            if not record_id or quantity < 1:
                raise ValueError("Each selected product needs a catalog identity and quantity.")
            if record_id in seen:
                raise ValueError("The same product was selected more than once.")
            seen.add(record_id)
            record = self._active_record(vendor, record_id)
            if record is None:
                raise FileNotFoundError("A selected product is no longer available.")
            offer = self._offer_from_record(vendor, record)
            available = offer.get("inventory")
            name = offer["name"]
            if available is not None and quantity > available:
                raise ValueError(
                    f"Only {available} {name} are currently available. Please update your quantity."
                )
            unit = int(offer["price_minor"])
            line = {
                "record_id": record["record_id"],
                "name": name,
                "brand": offer.get("brand"),
                "quantity": quantity,
                "unit_price_minor": unit,
                "subtotal_minor": unit * quantity,
                "currency": offer["currency"],
                "available": available,
            }
            displayed = item.get("displayed_price")
            if displayed is not None:
                try:
                    shown = Decimal(str(displayed)) * _MINOR_PER_MAJOR
                    line["displayed_price_minor"] = (
                        int(shown) if shown == shown.to_integral() else None
                    )
                except (InvalidOperation, ValueError):
                    line["displayed_price_minor"] = None
            if currency and line["currency"] != currency:
                raise ValueError("Selected products must share one currency.")
            currency = line["currency"]
            lines.append(line)
        return lines

    def _active_record(self, vendor: Document, record_id: str) -> Document | None:
        """Load one active record by the Phase 1 canonical identity only."""
        revision = vendor.get("active_sync_id")
        if not revision:
            return None
        return self.records.find_one(
            {
                "vendor_id": vendor["_id"],
                "sync_id": revision,
                "record_id": record_id,
            }
        )

    def _offer_from_record(self, vendor: Document, record: Mapping[str, Any]) -> Document:
        """Read current title, price, currency, and stock from catalog data, not the browser."""
        mapping = vendor.get("mapping")
        commerce = record.get("commerce")
        if not isinstance(commerce, Mapping):
            commerce = self.project_record(record, mapping) if mapping else None
        data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
        name = ""
        if isinstance(commerce, Mapping):
            name = str(commerce.get("title") or "").strip()
        if not name:
            aliases = self.config.mapping.aliases.get("title", ())
            for key in aliases:
                value = data.get(key)
                if value not in (None, ""):
                    name = str(value).strip()
                    break
        price_minor = commerce.get("price") if isinstance(commerce, Mapping) else None
        if not isinstance(price_minor, int):
            units = (mapping or {}).get("price_units")
            price_minor = _minor_price(data.get("price"), units)
        currency = ""
        if isinstance(commerce, Mapping):
            currency = str(commerce.get("currency") or "").strip().upper()
        if not currency:
            currency = str(data.get("currency") or (mapping or {}).get("default_currency") or "")
            currency = currency.strip().upper()
        if not name or not isinstance(price_minor, int) or not _CURRENCY.fullmatch(currency):
            raise ValueError(
                "This product cannot be purchased because its current price is unavailable."
            )
        brand = ""
        if isinstance(commerce, Mapping):
            brand = str(commerce.get("brand") or "").strip()
        if not brand:
            brand = str(data.get("brand") or "").strip()
        stock = None
        if isinstance(commerce, Mapping) and commerce.get("inventory") is not None:
            stock = _count(commerce.get("inventory"))
        if stock is None:
            for key in self.config.mapping.aliases.get("inventory", ()):
                stock = _count(data.get(key))
                if stock is not None:
                    break
        offer: Document = {
            "name": name,
            "price_minor": price_minor,
            "currency": currency,
            "inventory": stock,
        }
        if brand:
            offer["brand"] = brand
        return offer

    def _purchase_document(
        self, reference: VendorReference, attempt_id: str
    ) -> tuple[Document, Document]:
        """Resolve one vendor-scoped purchase attempt without exposing storage details."""
        vendor = self._find_vendor(reference)
        if not vendor:
            raise FileNotFoundError("The requested storefront was not found.")
        document = self.purchases.find_one(
            {"vendor_id": vendor["_id"], "attempt_id": str(attempt_id or "").strip()}
        )
        if document is None:
            raise FileNotFoundError("The requested purchase was not found.")
        return vendor, document

    def payable_amount(self, reference: VendorReference, attempt_id: str) -> tuple[int, str]:
        """Return the authoritative stored total in minor units for provider order creation."""
        _, document = self._purchase_document(reference, attempt_id)
        return int(document["total_minor"]), str(document["currency"])

    @staticmethod
    def _payment_state() -> Document:
        """Record that Phase 2 stops before provider execution and inventory mutation."""
        return {
            "started": False,
            "succeeded": False,
            "failed": False,
            "status": "not_started",
            "provider": None,
        }

    def _purchase_view(self, document: Mapping[str, Any]) -> Document:
        """Return shopper-safe purchase facts plus operator audit fields without secrets."""
        items = []
        stale = False
        for item in document.get("items") or []:
            displayed = item.get("displayed_price_minor")
            if displayed is not None and displayed != item.get("unit_price_minor"):
                stale = True
            items.append(
                {
                    "name": item.get("name"),
                    "brand": item.get("brand"),
                    "quantity": item.get("quantity"),
                    "unit_price": _major_units(int(item["unit_price_minor"])),
                    "subtotal": _major_units(int(item["subtotal_minor"])),
                    "currency": item.get("currency"),
                }
            )
        notices = [
            "Availability shown when selected.",
            "Current price will be verified.",
            "Current availability will be verified.",
            "You will be redirected to the payment provider.",
            "No payment has been made yet.",
        ]
        if stale:
            notices.insert(
                0,
                "The price shown earlier was a snapshot. The current catalog price is being used.",
            )
        payment = dict(document.get("payment") or self._payment_state())
        policy = dict(document.get("authorization") or {})
        authorization = None
        if policy:
            authorization = {
                "authorized": bool(policy.get("authorized")),
                "max_amount": _major_units(int(policy.get("max_amount_minor") or 0)),
                "currency": policy.get("currency"),
                "status": policy.get("status"),
                "expires_at": policy.get("expires_at"),
            }
        checkout = None
        if payment.get("started") and payment.get("razorpay_order_id"):
            checkout = {
                "order_id": payment.get("razorpay_order_id"),
                "amount": int(payment.get("amount_minor") or document.get("total_minor") or 0),
                "currency": payment.get("currency") or document.get("currency"),
            }
        return {
            "id": document["attempt_id"],
            "status": document["status"],
            "items": items,
            "total": _major_units(int(document["total_minor"])),
            "currency": document.get("currency"),
            "notices": notices,
            "payment": {
                "started": bool(payment.get("started")),
                "succeeded": bool(payment.get("succeeded")),
                "failed": bool(payment.get("failed")),
                "status": payment.get("status") or "not_started",
            },
            "authorization": authorization,
            "checkout": checkout,
            "authorized_at": document.get("authorized_at"),
            "cancelled_at": document.get("cancelled_at"),
        }

    def list_syncs(self, reference: VendorReference, limit: int = 20) -> list[Document]:
        """Return recent revision history without exposing source artifact bytes."""
        vendor = self._find_vendor(reference)
        if not vendor:
            return []
        size = self._page_size(limit)
        cursor = (
            self.syncs.find({"vendor_id": vendor["_id"]}).sort("started_at", DESCENDING).limit(size)
        )
        return [_public(item) for item in cursor]

    def stats(self, reference: VendorReference) -> Document:
        """Summarize only the currently published catalog revision."""
        vendor = self._find_vendor(reference)
        revision = (vendor or {}).get("active_sync_id")
        if not vendor or not revision:
            return {"resources": 0, "records": 0, "active_sync_id": None, "last_sync": None}
        match = {"vendor_id": vendor["_id"], "sync_id": revision}
        latest = self.syncs.find_one({"vendor_id": vendor["_id"], "sync_id": revision})
        return {
            "resources": self.resources.count_documents(match),
            "records": self.records.count_documents(match),
            "active_sync_id": revision,
            "last_sync": _public(latest) if latest else None,
        }

    def get_stats(self, reference: VendorReference) -> Document:
        """Keep a readable alias for integrations that prefer getter naming."""
        return self.stats(reference)

    def suggest_mapping(self, fields: Iterable[str]) -> Document:
        """Score configured aliases with RapidFuzz and select only a unique best match."""
        from rapidfuzz import fuzz, utils

        sources = list(dict.fromkeys(str(field) for field in fields))
        suggestions: Document = {}
        for target, aliases in self.config.mapping.aliases.items():
            ranked = sorted(
                (
                    (
                        source,
                        max(
                            fuzz.WRatio(source, alias, processor=utils.default_process)
                            for alias in aliases
                        ),
                    )
                    for source in sources
                ),
                key=lambda item: (-item[1], item[0]),
            )
            if not ranked:
                continue
            source, score = ranked[0]
            ambiguous = len(ranked) > 1 and score == ranked[1][1]
            suggestions[target] = {
                "field": source,
                "score": round(score, 2),
                "selected": score >= self.config.mapping.minimum_score and not ambiguous,
                "ambiguous": ambiguous,
            }
        return suggestions

    def project_record(self, record: Mapping[str, Any], mapping: Any) -> Document | None:
        """Project only explicitly mapped, complete product facts into a separate view."""
        rule, data = _payload(mapping), record.get("data")
        if not isinstance(data, Mapping):
            return None
        fields = rule.get("fields", {})
        if not isinstance(fields, Mapping):
            return None
        values = {target: _path_value(data, str(path)) for target, path in fields.items()}
        currency = values.get("currency") or rule.get("default_currency")
        price = _minor_price(values.get("price"), rule.get("price_units"))
        required = (
            values.get("id"),
            values.get("title"),
            values.get("description"),
            price,
            currency,
        )
        if any(value is None or value == "" for value in required) or not _CURRENCY.fullmatch(
            str(currency)
        ):
            return None
        projected: Document = {
            "id": str(values["id"]),
            "title": str(values["title"]),
            "description": values["description"],
            "price": price,
            "currency": str(currency).upper(),
        }
        for key in (
            "handle",
            "url",
            "category",
            "image",
            "rating",
            "availability",
            "inventory",
            "metadata",
        ):
            if values.get(key) is not None:
                projected[key] = values[key]
        return projected

    def project_product(self, record: Mapping[str, Any], mapping: Any) -> Document | None:
        """Expose the projection under the product-oriented integration name."""
        return self.project_record(record, mapping)

    def project_products(self, reference: VendorReference, mapping: Any | None = None) -> int:
        """Rebuild additive projections for one active revision without changing raw data."""
        vendor = self._find_vendor(reference)
        if not vendor or not vendor.get("active_sync_id"):
            return 0
        rule = _payload(mapping or vendor.get("mapping") or {})
        match = {"vendor_id": vendor["_id"], "sync_id": vendor["active_sync_id"]}
        self.records.update_many(match, {"$set": {"commerce": None}})
        match["resource"] = rule.get("resource")
        projected, operations = 0, []
        for record in self.records.find(match):
            commerce = self.project_record(record, rule)
            if commerce:
                operations.append(
                    UpdateOne({"_id": record["_id"]}, {"$set": {"commerce": commerce}})
                )
                projected += 1
            if len(operations) == self.config.limits.batch_size:
                self.records.bulk_write(operations, ordered=False)
                operations = []
        if operations:
            self.records.bulk_write(operations, ordered=False)
        status = "ready" if projected else "needs_mapping"
        self.vendors.update_one(
            {"_id": vendor["_id"]}, {"$set": {"status": status, "updated_at": datetime.now(UTC)}}
        )
        return projected

    def start_sync(self, reference: VendorReference, kind: str, digest: str, size: int) -> Document:
        """Create a staging revision while leaving the prior active revision untouched."""
        vendor = self._find_vendor(reference)
        if not vendor:
            raise FileNotFoundError(f"Vendor {reference} does not exist.")
        sync_id, now = uuid4().hex, datetime.now(UTC)
        document = {
            "sync_id": sync_id,
            "vendor_id": vendor["_id"],
            "status": "running",
            "adapter": kind,
            "source_digest": digest,
            "source_size": size,
            "started_at": now,
            "previous_sync_id": vendor.get("active_sync_id"),
            "previous_status": vendor.get("status", "registered"),
        }
        self.syncs.insert_one(document)
        self.vendors.update_one(
            {"_id": vendor["_id"]}, {"$set": {"status": "syncing", "updated_at": now}}
        )
        return _public(document)

    def store_artifact(
        self, reference: VendorReference, sync_id: str, source: Path, digest: str
    ) -> str:
        """Retain exact source bytes in deduplicated GridFS storage keyed by vendor/digest."""
        vendor = self._find_vendor(reference)
        if not vendor:
            raise FileNotFoundError(f"Vendor {reference} does not exist.")
        files = self.database[f"{self.artifact_bucket_name}.files"]
        query = {"metadata.vendor_id": vendor["_id"], "metadata.digest": digest}
        existing = files.find_one(query)
        if existing:
            files.update_one(
                {"_id": existing["_id"]}, {"$addToSet": {"metadata.sync_ids": sync_id}}
            )
            self.syncs.update_one({"sync_id": sync_id}, {"$set": {"artifact_id": existing["_id"]}})
            return str(existing["_id"])
        metadata = {"vendor_id": vendor["_id"], "digest": digest, "sync_ids": [sync_id]}
        with source.open("rb") as stream:
            artifact_id = self.artifacts.upload_from_stream(source.name, stream, metadata=metadata)
        self.syncs.update_one({"sync_id": sync_id}, {"$set": {"artifact_id": artifact_id}})
        return str(artifact_id)

    def write_records(self, sync_id: str, records: Iterable[Document]) -> int:
        """Bulk-upsert one staging batch under revision-specific physical identifiers."""
        documents = list(records)
        sync = self.syncs.find_one({"sync_id": sync_id}, {"vendor_id": 1})
        if not sync:
            raise ValueError(f"Sync revision {sync_id} does not exist.")
        operations = [
            ReplaceOne(
                {"_id": f"{sync_id}:{item['record_id']}"},
                {
                    **item,
                    "_id": f"{sync_id}:{item['record_id']}",
                    "vendor_id": sync["vendor_id"],
                    "sync_id": sync_id,
                },
                upsert=True,
            )
            for item in documents
        ]
        if operations:
            self.records.bulk_write(operations, ordered=False)
        return len(documents)

    def publish_sync(
        self,
        reference: VendorReference,
        sync_id: str,
        resources: Iterable[Document],
        counts: Mapping[str, int],
        warnings: Iterable[str],
    ) -> Document:
        """Publish completed metadata, swap the active revision, then prune stale revisions."""
        vendor = self._find_vendor(reference)
        if not vendor:
            raise FileNotFoundError(f"Vendor {reference} does not exist.")
        resource_documents = list(resources)
        now = datetime.now(UTC)
        operations = [
            ReplaceOne(
                {"vendor_id": vendor["_id"], "sync_id": sync_id, "name": item["name"]},
                {
                    **item,
                    "vendor_id": vendor["_id"],
                    "sync_id": sync_id,
                    "published_at": now,
                },
                upsert=True,
            )
            for item in resource_documents
        ]
        if operations:
            self.resources.bulk_write(operations, ordered=False)
        status = "ready" if counts.get("projected", 0) else "needs_mapping"
        self.syncs.update_one(
            {"vendor_id": vendor["_id"], "sync_id": sync_id},
            {
                "$set": {
                    "status": "succeeded",
                    "counts": dict(counts),
                    "warnings": list(warnings),
                    "finished_at": now,
                }
            },
        )
        self.vendors.update_one(
            {"_id": vendor["_id"]},
            {
                "$set": {"active_sync_id": sync_id, "status": status, "updated_at": now},
                "$unset": {"last_sync_error": ""},
            },
        )
        stale = {"vendor_id": vendor["_id"], "sync_id": {"$ne": sync_id}}
        try:
            self.records.delete_many(stale)
            self.resources.delete_many(stale)
        except PyMongoError:
            self.syncs.update_one(
                {"vendor_id": vendor["_id"], "sync_id": sync_id},
                {
                    "$push": {"warnings": "An inactive revision could not be fully pruned."},
                    "$inc": {"counts.warnings": 1},
                },
            )
        document = self.syncs.find_one({"vendor_id": vendor["_id"], "sync_id": sync_id})
        return _public(document) if document else {}

    def fail_sync(self, reference: VendorReference, sync_id: str, error: Exception) -> None:
        """Discard only failed staging data and preserve the last published revision."""
        vendor = self._find_vendor(reference)
        if not vendor:
            return
        match = {"vendor_id": vendor["_id"], "sync_id": sync_id}
        self.records.delete_many(match)
        self.resources.delete_many(match)
        now = datetime.now(UTC)
        self.syncs.update_one(
            match,
            {
                "$set": {
                    "status": "failed",
                    "error": {"type": type(error).__name__, "message": str(error)},
                    "finished_at": now,
                }
            },
        )
        sync = self.syncs.find_one(match) or {}
        previous = sync.get("previous_sync_id")
        active_filter: Document = {"active_sync_id": {"$in": [sync_id, previous]}}
        if previous is None:
            active_filter = {
                "$or": [
                    {"active_sync_id": sync_id},
                    {"active_sync_id": None},
                    {"active_sync_id": {"$exists": False}},
                ]
            }
        update: Document = {
            "$set": {
                "status": sync.get("previous_status", "registered"),
                "last_sync_error": {"type": type(error).__name__, "message": str(error)},
                "updated_at": now,
            }
        }
        if previous is None:
            update["$unset"] = {"active_sync_id": ""}
        else:
            update["$set"]["active_sync_id"] = previous
        self.vendors.update_one({"_id": vendor["_id"], **active_filter}, update)

    def _find_vendor(self, reference: VendorReference) -> Document | None:
        """Find the exact stored vendor key before coercing valid ObjectId strings."""
        if isinstance(reference, ObjectId):
            return self.vendors.find_one({"_id": reference})
        choices: list[Any] = [reference]
        if ObjectId.is_valid(reference):
            choices.append(ObjectId(reference))
        return self.vendors.find_one({"$or": [{"_id": {"$in": choices}}, {"slug": reference}]})

    def _vendor_view(self, document: Mapping[str, Any]) -> Document:
        """Add current source/slug defaults to a copy of a legacy vendor document."""
        result = dict(document)
        result["source"] = self._source(result)
        result["slug"] = (
            result.get("slug") or slugify(str(result.get("name", ""))) or str(result["_id"])
        )
        result.setdefault("public", True)
        result.setdefault("status", "registered")
        return _public(result)

    def _source(self, document: Mapping[str, Any]) -> Document:
        """Translate legacy format/db_path fields into the nested source shape."""
        source = dict(document.get("source") or {})
        kind = source.get("kind") or document.get("format") or document.get("type")
        path = source.get("path") or document.get("db_path") or document.get("location")
        return {**source, "kind": str(kind or "").lower(), "path": str(path or "")}

    def _record_page(
        self,
        reference: VendorReference,
        resource: str | None,
        cursor: str | None,
        limit: int | None,
        query: str | None,
        commerce_only: bool,
    ) -> Document:
        """Build one bounded active-revision query and a stable continuation cursor."""
        vendor = self._find_vendor(reference)
        revision = (vendor or {}).get("active_sync_id")
        if not vendor or not revision:
            return {"items": [], "next_cursor": None, "total": 0}
        if cursor is not None and not _CURSOR.fullmatch(cursor):
            raise ValueError("Record cursor is malformed.")
        match: Document = {"vendor_id": vendor["_id"], "sync_id": revision}
        if resource:
            match["resource"] = resource
        if commerce_only:
            match["commerce"] = {"$ne": None}
        if query:
            if not query.strip():
                raise ValueError("Search query cannot be empty.")
            if len(query) > self.config.limits.max_query_length:
                raise ValueError("Search query exceeds the configured limit.")
            match["$text"] = {"$search": query}
        total, page_match = self.records.count_documents(match), dict(match)
        if cursor:
            page_match["record_id"] = {"$gt": cursor}
        size = self._page_size(limit)
        found = list(self.records.find(page_match).sort("record_id", ASCENDING).limit(size + 1))
        items, more = found[:size], len(found) > size
        return {
            "items": [self._record_view(item) for item in items],
            "next_cursor": items[-1]["record_id"] if more and items else None,
            "total": total,
        }

    def _record_view(self, document: Mapping[str, Any]) -> Document:
        """Hide revision-specific storage IDs and expose the stable record ID as `_id`."""
        result = dict(document)
        result["_id"] = result.pop("record_id")
        return _public(result)

    def _page_size(self, limit: int | None) -> int:
        """Apply centralized pagination defaults and fail on invalid bounds."""
        value = self.config.limits.default_page_size if limit is None else limit
        if value < 1 or value > self.config.limits.max_page_size:
            raise ValueError("Page size is outside the configured bounds.")
        return value
