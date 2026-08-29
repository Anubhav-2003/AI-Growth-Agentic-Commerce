from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from bson import ObjectId
from gridfs import GridFSBucket
from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel, ReplaceOne, UpdateOne
from pymongo.database import Database
from pymongo.errors import PyMongoError
from slugify import slugify

from config import CommerceConfig

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
        for collection in (self.resources, self.records, self.syncs):
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
