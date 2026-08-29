"""Lossless normalization and revision tests against an isolated real Mongo database."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from bson import ObjectId
from pymongo import MongoClient
from pymongo.database import Database

from config import CommerceConfig, get_settings
from services.catalog_service import CatalogService
from services.normalization_service import NormalizationService


@pytest.fixture
def commerce_config() -> CommerceConfig:
    """Load production's validated non-secret service configuration."""
    path = Path(__file__).resolve().parents[1] / "config" / "commerce.yml"
    return CommerceConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@pytest.fixture
def mongo_database() -> Database[dict[str, Any]]:
    """Yield one uniquely named real database and drop only that exact database."""
    settings = get_settings()
    client: MongoClient[dict[str, Any]] = MongoClient(
        settings.mongodb_uri, serverSelectionTimeoutMS=10_000
    )
    database_name = f"commerceos_pytest_{uuid4().hex}"
    try:
        client.admin.command("ping")
        yield client[database_name]
    finally:
        client.drop_database(database_name)
        client.close()


@pytest.fixture
def services(
    tmp_path: Path,
    commerce_config: CommerceConfig,
    mongo_database: Database[dict[str, Any]],
) -> tuple[Path, CatalogService, NormalizationService]:
    """Build real services over one temporary allow-listed source root."""
    root = tmp_path / "sources"
    root.mkdir()
    catalog = CatalogService(mongo_database, commerce_config)
    catalog.ensure_indexes()
    normalizer = NormalizationService(
        catalog, [root], commerce_config.formats, commerce_config.limits
    )
    return root, catalog, normalizer


def _create_vendor(
    catalog: CatalogService, name: str, kind: str, path: Path, mapping: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Register one test vendor through the public current-shape API."""
    payload: dict[str, Any] = {
        "name": name,
        "slug": f"{name.lower().replace(' ', '-')}-{uuid4().hex[:8]}",
        "source": {"kind": kind, "path": str(path)},
    }
    if mapping:
        payload["mapping"] = mapping
    return catalog.create_vendor(payload)


def test_csv_preserves_duplicate_headers_ragged_cells_and_search(
    services: tuple[Path, CatalogService, NormalizationService],
) -> None:
    """Retain original CSV positions while exposing collision-safe convenience keys."""
    root, catalog, normalizer = services
    source = root / "ragged.csv"
    source.write_text('name,name,,qty\n"Alpha","Alias",,1,extra\n"Beta","",\n', encoding="utf-8")
    vendor = _create_vendor(catalog, "CSV Fidelity", "csv", source)

    summary = normalizer.run(vendor["_id"])
    resource = catalog.get_resource(vendor["_id"], "ragged")
    records = catalog.list_records(vendor["_id"], resource="ragged", limit=10)["items"]
    records.sort(key=lambda item: item["source"]["position"])

    assert summary["counts"] == {
        "resources": 1,
        "records": 2,
        "written": 2,
        "projected": 0,
        "warnings": 2,
    }
    assert resource and resource["schema"]["headers"] == ["name", "name", "", "qty"]
    assert records[0]["source"]["cells"] == ["Alpha", "Alias", "", "1", "extra"]
    assert records[0]["data"] == {
        "name": "Alpha",
        "name__2": "Alias",
        "_column_3": "",
        "qty": "1",
        "_column_5": "extra",
    }
    assert "qty" not in records[1]["data"] and records[1]["data"]["name__2"] == ""
    assert catalog.search_records(vendor["_id"], "Alias", limit=10)["total"] == 1
    with pytest.raises(ValueError, match="cursor"):
        catalog.list_records(vendor["_id"], cursor="not-a-cursor")


def test_json_preserves_nested_values_duplicate_members_and_digest(
    services: tuple[Path, CatalogService, NormalizationService],
) -> None:
    """Encode duplicate members reversibly and retain the exact artifact digest."""
    root, catalog, normalizer = services
    source = root / "nested.json"
    raw = (
        '{"products":[{"id":"p1","details":{"color":"red","color":"blue"},'
        '"tags":["new",{"rank":1}],"price":0.10}],"meta":{"version":1}}'
    )
    source.write_text(raw, encoding="utf-8")
    vendor = _create_vendor(catalog, "JSON Fidelity", "json", source)

    summary = normalizer.run(vendor["_id"])
    resources = {item["name"] for item in catalog.list_resources(vendor["_id"])}
    product = catalog.list_records(vendor["_id"], resource="products", limit=10)["items"][0]

    assert resources == {"products", "metadata"}
    assert summary["source_digest"] == hashlib.sha256(raw.encode()).hexdigest()
    assert summary["counts"]["warnings"] == 1
    assert product["data"]["details"] == {
        "$commerceos_type": "json_object",
        "pairs": [
            {"key": "color", "value": "red"},
            {"key": "color", "value": "blue"},
        ],
    }
    assert product["data"]["tags"] == ["new", {"rank": 1}]
    assert product["data"]["price"] == {"$commerceos_type": "decimal", "value": "0.10"}


def test_sqlite_extracts_all_tables_keys_blobs_and_relationships(
    services: tuple[Path, CatalogService, NormalizationService],
) -> None:
    """Reflect every table and attach exact foreign-key values to related rows."""
    root, catalog, normalizer = services
    source = root / "catalog.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE products (
                sku TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                payload BLOB,
                legacy_code TEXT UNIQUE
            );
            CREATE TABLE features (
                sku TEXT NOT NULL,
                position INTEGER NOT NULL,
                feature TEXT NOT NULL,
                PRIMARY KEY (sku, position),
                FOREIGN KEY (sku) REFERENCES products(sku) ON DELETE CASCADE
            );
            CREATE TABLE inventory (
                sku TEXT PRIMARY KEY,
                quantity INTEGER NOT NULL,
                FOREIGN KEY (sku) REFERENCES products(sku)
            );
            CREATE TABLE non_pk_refs (
                id INTEGER PRIMARY KEY,
                legacy_code TEXT NOT NULL,
                FOREIGN KEY (legacy_code) REFERENCES products(legacy_code)
            );
            CREATE TABLE composite_targets (
                tenant TEXT NOT NULL,
                sku TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                PRIMARY KEY (tenant, sku)
            );
            CREATE TABLE partial_refs (
                id INTEGER PRIMARY KEY,
                sku TEXT NOT NULL,
                FOREIGN KEY (sku) REFERENCES composite_targets(sku)
            );
            """
        )
        connection.execute(
            "INSERT INTO products VALUES (?, ?, ?, ?)",
            ("p1", "Lamp", b"\x00\xff", "legacy-p1"),
        )
        connection.execute("INSERT INTO features VALUES (?, ?, ?)", ("p1", 0, "Bright"))
        connection.execute("INSERT INTO inventory VALUES (?, ?)", ("p1", 7))
        connection.execute("INSERT INTO non_pk_refs VALUES (?, ?)", (1, "legacy-p1"))
        connection.execute(
            "INSERT INTO composite_targets VALUES (?, ?, ?)", ("tenant-1", "p2", "Desk")
        )
        connection.execute("INSERT INTO partial_refs VALUES (?, ?)", (1, "p2"))
    vendor = _create_vendor(catalog, "SQLite Fidelity", "sqlite", source)

    summary = normalizer.run(vendor["_id"])
    resources = {item["name"]: item for item in catalog.list_resources(vendor["_id"])}
    feature = catalog.list_records(vendor["_id"], resource="features", limit=10)["items"][0]
    product = catalog.list_records(vendor["_id"], resource="products", limit=10)["items"][0]
    non_pk = catalog.list_records(vendor["_id"], resource="non_pk_refs", limit=10)["items"][0]
    partial = catalog.list_records(vendor["_id"], resource="partial_refs", limit=10)["items"][0]

    assert summary["counts"]["resources"] == 6
    assert summary["counts"]["records"] == 6
    assert set(resources) == {
        "products",
        "features",
        "inventory",
        "non_pk_refs",
        "composite_targets",
        "partial_refs",
    }
    assert resources["features"]["schema"]["primary_key"] == ["sku", "position"]
    assert resources["features"]["schema"]["foreign_keys"][0]["target_resource"] == "products"
    assert feature["relationships"][0]["local"] == {"sku": "p1"}
    assert feature["relationships"][0]["target"] == {"sku": "p1"}
    assert feature["relationships"][0]["target_id"] == product["_id"]
    assert catalog.get_record(vendor["_id"], feature["relationships"][0]["target_id"]) == product
    assert non_pk["relationships"][0]["target"] == {"legacy_code": "legacy-p1"}
    assert "target_id" not in non_pk["relationships"][0]
    assert partial["relationships"][0]["target"] == {"sku": "p2"}
    assert "target_id" not in partial["relationships"][0]
    assert product["data"]["payload"] == {
        "$commerceos_type": "binary",
        "encoding": "base64",
        "content_type": "application/octet-stream",
        "value": "AP8=",
    }


def test_resync_keeps_stable_ids_and_prunes_old_physical_revision(
    services: tuple[Path, CatalogService, NormalizationService],
) -> None:
    """Keep public IDs stable while replacing the inactive physical revision."""
    root, catalog, normalizer = services
    source = root / "stable.csv"
    source.write_text("sku,title\np1,One\np2,Two\n", encoding="utf-8")
    vendor = _create_vendor(catalog, "Stable IDs", "csv", source)

    first = normalizer.run(vendor["_id"])
    first_ids = {
        item["source"]["position"]: item["_id"]
        for item in catalog.list_records(vendor["_id"], limit=10)["items"]
    }
    second = normalizer.run(vendor["_id"])
    second_ids = {
        item["source"]["position"]: item["_id"]
        for item in catalog.list_records(vendor["_id"], limit=10)["items"]
    }

    assert first["sync_id"] != second["sync_id"]
    assert first_ids == second_ids
    assert catalog.records.count_documents({"vendor_id": ObjectId(vendor["_id"])}) == 2
    assert len(catalog.list_syncs(vendor["_id"])) == 2


def test_projection_requires_explicit_complete_units_and_never_rounds(
    services: tuple[Path, CatalogService, NormalizationService],
) -> None:
    """Convert explicit major/minor units while rejecting ambiguous or rounded prices."""
    _, catalog, _ = services
    record = {
        "data": {
            "sku": "p1",
            "name": "Lamp",
            "copy": "A lamp",
            "amount": "12.34",
            "currency": "usd",
        }
    }
    fields = {
        "id": "sku",
        "title": "name",
        "description": "copy",
        "price": "amount",
        "currency": "currency",
    }

    major = catalog.project_record(record, {"fields": fields, "price_units": "major"})
    record["data"]["amount"] = 1234
    minor = catalog.project_record(record, {"fields": fields, "price_units": "minor"})
    record["data"]["amount"] = "12.345"

    assert major and major["price"] == 1234 and major["currency"] == "USD"
    assert minor and minor["price"] == 1234
    assert catalog.project_record(record, {"fields": fields, "price_units": "major"}) is None
    assert catalog.project_record(record, {"fields": fields}) is None


def test_path_escape_and_symlink_escape_are_rejected(
    services: tuple[Path, CatalogService, NormalizationService], tmp_path: Path
) -> None:
    """Reject both direct and symlinked files outside the configured source root."""
    root, catalog, normalizer = services
    outside = tmp_path / "outside.csv"
    outside.write_text("id\n1\n", encoding="utf-8")
    direct = _create_vendor(catalog, "Direct Escape", "csv", outside)
    with pytest.raises(PermissionError, match="outside"):
        normalizer.run(direct["_id"])

    symlink = root / "escape.csv"
    symlink.symlink_to(outside)
    linked = _create_vendor(catalog, "Symlink Escape", "csv", symlink)
    with pytest.raises(PermissionError, match="outside"):
        normalizer.run(linked["_id"])
    assert catalog.syncs.count_documents({}) == 0


def test_failed_sync_discards_staging_and_preserves_active_revision(
    services: tuple[Path, CatalogService, NormalizationService], commerce_config: CommerceConfig
) -> None:
    """Delete a partially written failed revision without hiding the last success."""
    root, catalog, normalizer = services
    source = root / "good.csv"
    source.write_text("sku,title\np1,One\np2,Two\n", encoding="utf-8")
    vendor = _create_vendor(catalog, "Atomic Sync", "csv", source)
    successful = normalizer.run(vendor["_id"])
    stable_ids = {item["_id"] for item in catalog.list_records(vendor["_id"], limit=10)["items"]}

    broken = root / "broken.csv"
    broken.write_text("sku,title\np1,One\np2,Two\np3," + "x" * 5_000 + "\n", encoding="utf-8")
    catalog.update_vendor(vendor["_id"], {"source": {"kind": "csv", "path": str(broken)}})
    limits = commerce_config.limits.model_copy(update={"batch_size": 2, "max_record_bytes": 2_048})
    failing = NormalizationService(catalog, [root], commerce_config.formats, limits)

    with pytest.raises(ValueError, match="size limit"):
        failing.run(vendor["_id"])

    current = catalog.get_vendor(vendor["_id"])
    history = catalog.list_syncs(vendor["_id"])
    failed = next(item for item in history if item["status"] == "failed")
    current_ids = {item["_id"] for item in catalog.list_records(vendor["_id"], limit=10)["items"]}
    assert current and current["active_sync_id"] == successful["sync_id"]
    assert current_ids == stable_ids
    assert catalog.records.count_documents({"sync_id": failed["sync_id"]}) == 0
    assert {item["status"] for item in history} == {"succeeded", "failed"}


def test_legacy_vendor_shape_remains_queryable(
    services: tuple[Path, CatalogService, NormalizationService],
) -> None:
    """Read legacy fields additively without rewriting or dropping their original values."""
    root, catalog, _ = services
    identifier = catalog.vendors.insert_one(
        {
            "name": "Legacy Store",
            "type": "local",
            "format": "csv",
            "location": "Bengaluru",
            "db_path": str(root / "legacy.csv"),
        }
    ).inserted_id

    vendor = catalog.get_vendor(str(identifier))
    by_slug = catalog.get_vendor_by_slug("legacy-store", public_only=True)

    assert vendor and vendor["source"] == {
        "kind": "csv",
        "path": str(root / "legacy.csv"),
    }
    assert vendor["format"] == "csv" and vendor["location"] == "Bengaluru"
    assert by_slug and by_slug["_id"] == str(identifier)
