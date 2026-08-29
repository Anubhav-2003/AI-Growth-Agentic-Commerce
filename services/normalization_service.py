from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from bson import BSON
from genson import SchemaBuilder

from config import LimitConfig
from services.catalog_service import CatalogService, Document, VendorReference

_BSON_INT_MIN = -(2**63)
_BSON_INT_MAX = 2**63 - 1


class _JSONObject(dict[str, Any]):
    """Keep JSON member order and duplicates until they receive a reversible envelope."""

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        """Retain every parsed pair while providing normal last-member dictionary access."""
        super().__init__(pairs)
        self.pairs = pairs


@dataclass(slots=True)
class _Record:
    """Carry one adapter-neutral source record into publication."""

    data: Mapping[str, Any]
    source: Document
    relationships: list[Document]


@dataclass(slots=True)
class _Resource:
    """Describe one source table/list and its streaming records."""

    name: str
    kind: str
    records: Iterable[_Record]
    schema: Document
    warnings: list[str]


def _canonical(value: Any) -> str:
    """Serialize encoded values deterministically for hashes and identities."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    """Return a stable SHA-256 digest for one canonical encoded value."""
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _record_id(vendor_id: str, resource: str, identity: Any) -> str:
    """Derive the stable ID shared by records and resolvable relationship targets."""
    return _digest({"vendor_id": vendor_id, "resource": resource, "identity": identity})


def _encode(value: Any) -> Any:
    """Convert only BSON-incompatible values into explicit reversible envelopes."""
    if isinstance(value, _JSONObject):
        pairs = [(key, _encode(item)) for key, item in value.pairs]
        duplicate = len({key for key, _ in pairs}) != len(pairs)
        if duplicate or any("\x00" in key for key, _ in pairs):
            return {
                "$commerceos_type": "json_object",
                "pairs": [{"key": key, "value": item} for key, item in pairs],
            }
        return {key: item for key, item in pairs}
    if isinstance(value, Mapping):
        pairs = [(str(key), _encode(item)) for key, item in value.items()]
        if any("\x00" in key for key, _ in pairs):
            return {
                "$commerceos_type": "object",
                "pairs": [{"key": key, "value": item} for key, item in pairs],
            }
        return dict(pairs)
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    if isinstance(value, bytes):
        return {
            "$commerceos_type": "binary",
            "encoding": "base64",
            "content_type": "application/octet-stream",
            "value": base64.b64encode(value).decode(),
        }
    if isinstance(value, Decimal):
        return {"$commerceos_type": "decimal", "value": str(value)}
    if isinstance(value, int) and not _BSON_INT_MIN <= value <= _BSON_INT_MAX:
        return {"$commerceos_type": "integer", "value": str(value)}
    if isinstance(value, float) and not math.isfinite(value):
        return {"$commerceos_type": "float", "value": str(value)}
    if isinstance(value, (datetime, date, time)):
        return {"$commerceos_type": type(value).__name__, "value": value.isoformat()}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Unsupported source value type: {type(value).__name__}")


def _scalar_text(value: Any) -> Iterator[str]:
    """Flatten field names and scalar values into deterministic searchable text."""
    if isinstance(value, Mapping):
        binary = value.get("$commerceos_type") == "binary"
        for key, item in value.items():
            yield str(key)
            if not (binary and key == "value"):
                yield from _scalar_text(item)
    elif isinstance(value, list):
        for item in value:
            yield from _scalar_text(item)
    elif value is not None:
        yield str(value)


def _column_keys(headers: Sequence[str], width: int | None = None) -> list[str]:
    """Create collision-safe convenience keys without changing the original headers."""
    names, used = [], set()
    for index in range(max(len(headers), width or 0)):
        base = headers[index] if index < len(headers) and headers[index] else f"_column_{index + 1}"
        candidate, suffix = base, 2
        while candidate in used:
            candidate, suffix = f"{base}__{suffix}", suffix + 1
        names.append(candidate)
        used.add(candidate)
    return names


def _quote(identifier: str) -> str:
    """Quote an identifier obtained only from trusted SQLite metadata."""
    return '"' + identifier.replace('"', '""') + '"'


def _reject_constant(value: str) -> None:
    """Reject non-standard NaN and infinity tokens instead of silently accepting them."""
    raise ValueError(f"JSON contains non-standard numeric constant {value}.")


def _duplicate_warnings(value: Any, path: str = "$") -> list[str]:
    """Report every duplicate JSON member path while the original pairs still exist."""
    warnings: list[str] = []
    if isinstance(value, _JSONObject):
        seen: set[str] = set()
        for key, item in value.pairs:
            if key in seen:
                warnings.append(
                    f"Duplicate JSON member at {path}.{key}; retained as ordered pairs."
                )
            seen.add(key)
            warnings.extend(_duplicate_warnings(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            warnings.extend(_duplicate_warnings(item, f"{path}[{index}]"))
    return warnings


class NormalizationService:
    """Normalize CSV, JSON, and every SQLite table without generative inference."""

    def __init__(
        self,
        catalog: CatalogService,
        source_roots: Sequence[Path],
        formats: Mapping[str, Sequence[str]],
        limits: LimitConfig,
    ) -> None:
        """Resolve allow-listed roots once and retain centralized format/size limits."""
        if not source_roots:
            raise ValueError("At least one source root is required.")
        self.catalog = catalog
        self.source_roots = tuple(root.expanduser().resolve() for root in source_roots)
        self.formats = {
            kind.lower(): tuple(ext.lower() for ext in extensions)
            for kind, extensions in formats.items()
        }
        self.limits = limits

    def run(self, vendor_ref: VendorReference) -> Document:
        """Stage, validate, and publish one complete revision or preserve the prior one."""
        vendor = self.catalog.get_vendor(vendor_ref)
        if not vendor:
            raise FileNotFoundError(f"Vendor {vendor_ref} does not exist.")
        source = vendor.get("source") or {}
        path = self._resolve_source(str(source.get("path", "")))
        kind = self._source_kind(str(source.get("kind", "")), path)
        size, digest = path.stat().st_size, self._file_digest(path)
        sync = self.catalog.start_sync(vendor["_id"], kind, digest, size)
        try:
            artifact_id = self.catalog.store_artifact(vendor["_id"], sync["sync_id"], path, digest)
            resource_docs, warnings = [], []
            counts = {"resources": 0, "records": 0, "written": 0, "projected": 0}
            for resource in self._resources(path, kind):
                document, written, projected = self._normalize_resource(
                    vendor, sync["sync_id"], resource
                )
                resource_docs.append(document)
                warnings.extend(resource.warnings)
                counts["resources"] += 1
                counts["records"] += document["record_count"]
                counts["written"] += written
                counts["projected"] += projected
            if self._file_digest(path) != digest:
                raise RuntimeError(
                    "Source changed during synchronization; retry with a stable file."
                )
            warnings = list(dict.fromkeys(warnings))
            counts["warnings"] = len(warnings)
            summary = self.catalog.publish_sync(
                vendor["_id"], sync["sync_id"], resource_docs, counts, warnings
            )
            return {**summary, "artifact_id": artifact_id}
        except Exception as error:
            self.catalog.fail_sync(vendor["_id"], sync["sync_id"], error)
            raise

    def _resolve_source(self, source_path: str) -> Path:
        """Resolve a file and prove symlinks cannot escape the configured roots."""
        if not source_path.strip():
            raise ValueError("Vendor source path is empty.")
        raw = Path(source_path).expanduser()
        candidates = [raw] if raw.is_absolute() else [Path.cwd() / raw]
        if not raw.is_absolute():
            candidates.extend(root / raw for root in self.source_roots)
            candidates.extend(root.parent / raw for root in self.source_roots)
        resolved = list(dict.fromkeys(candidate.resolve() for candidate in candidates))
        allowed = [
            candidate
            for candidate in resolved
            if any(candidate.is_relative_to(root) for root in self.source_roots)
        ]
        existing = next((candidate for candidate in allowed if candidate.is_file()), None)
        if existing:
            if existing.stat().st_size > self.limits.max_source_bytes:
                raise ValueError("Source file exceeds the configured size limit.")
            return existing
        if any(candidate.exists() for candidate in resolved):
            raise PermissionError("Source path is outside the configured roots.")
        raise FileNotFoundError(f"Source file does not exist: {source_path}")

    def _source_kind(self, declared: str, path: Path) -> str:
        """Confirm the declared adapter agrees with a configured file extension."""
        extension = path.suffix.lower()
        actual = next(
            (kind for kind, extensions in self.formats.items() if extension in extensions), None
        )
        tokens = {
            kind: {kind, *(item.removeprefix(".") for item in extensions)}
            for kind, extensions in self.formats.items()
        }
        expected = next(
            (kind for kind, aliases in tokens.items() if declared.lower() in aliases), None
        )
        if not actual:
            raise ValueError(f"Unsupported source extension: {extension}")
        if declared and expected != actual:
            raise ValueError("Declared source kind does not match its configured extension.")
        return actual

    def _file_digest(self, path: Path) -> str:
        """Hash exact source bytes with the standard streaming file-digest API."""
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _resources(self, path: Path, kind: str) -> list[_Resource]:
        """Dispatch to one deterministic standard-library adapter."""
        adapters = {
            "csv": self._csv_resources,
            "json": self._json_resources,
            "sqlite": self._sqlite_resources,
        }
        if kind not in adapters:
            raise ValueError(f"Unsupported source kind: {kind}")
        return adapters[kind](path)

    def _normalize_resource(
        self, vendor: Mapping[str, Any], sync_id: str, resource: _Resource
    ) -> tuple[Document, int, int]:
        """Build observed schema and write bounded batches for one source resource."""
        builder, batch, count, written, projected = SchemaBuilder(), [], 0, 0, 0
        mapping = vendor.get("mapping") or {}
        for item in resource.records:
            record = self._record_document(vendor["_id"], sync_id, resource.name, item)
            if isinstance(mapping, Mapping) and mapping.get("resource") == resource.name:
                record["commerce"] = self.catalog.project_record(record, mapping)
                projected += int(record["commerce"] is not None)
            builder.add_object(record["data"])
            if len(BSON.encode(record)) > self.limits.max_record_bytes:
                raise ValueError(f"Record {record['record_id']} exceeds the configured size limit.")
            batch.append(record)
            count += 1
            if len(batch) == self.limits.batch_size:
                written += self.catalog.write_records(sync_id, batch)
                batch = []
        written += self.catalog.write_records(sync_id, batch)
        schema = {**resource.schema, "observed": builder.to_schema()}
        fields = [field.get("storage_name", field["name"]) for field in schema.get("fields", [])]
        fields = fields or list(schema["observed"].get("properties", {}))
        document = {
            "name": resource.name,
            "kind": resource.kind,
            "schema": schema,
            "record_count": count,
            "mapping_suggestions": self.catalog.suggest_mapping(fields),
        }
        return document, written, projected

    def _record_document(
        self, vendor_id: str, sync_id: str, resource: str, item: _Record
    ) -> Document:
        """Create a lossless envelope with stable identity and additive projection slot."""
        data, source = _encode(item.data), _encode(item.source)
        identity = source.get("identity") if isinstance(source, dict) else None
        if not identity:
            identity = {"hash": _digest(data), "position": source["position"]}
            source["identity"] = identity
        record_id = _record_id(vendor_id, resource, identity)
        relationships = _encode(item.relationships)
        for relationship in relationships:
            if relationship.pop("_targets_primary_key", False):
                relationship["target_id"] = _record_id(
                    vendor_id,
                    relationship["target_resource"],
                    relationship["target"],
                )
        return {
            "record_id": record_id,
            "vendor_id": vendor_id,
            "resource": resource,
            "source": source,
            "data": data,
            "relationships": relationships,
            "search_text": "\n".join(_scalar_text(data)),
            "commerce": None,
            "sync_id": sync_id,
        }

    def _csv_resources(self, path: Path) -> list[_Resource]:
        """Describe one CSV resource while retaining headers and positional cells."""
        with path.open(newline="", encoding="utf-8-sig") as stream:
            sample = stream.read(8192)
            stream.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(stream, dialect)
            headers = next(reader, None)
        if headers is None:
            raise ValueError(f"CSV source has no header row: {path.name}")
        keys, warnings = _column_keys(headers), []
        fields = [
            {"name": name, "storage_name": key, "type": "string"}
            for name, key in zip(headers, keys, strict=True)
        ]
        schema = {
            "fields": fields,
            "headers": headers,
            "primary_key": [],
            "foreign_keys": [],
            "dialect": {
                "delimiter": dialect.delimiter,
                "quotechar": dialect.quotechar,
                "escapechar": dialect.escapechar,
            },
        }
        rows = self._csv_rows(path, dialect, headers, warnings)
        return [_Resource(path.stem, "table", rows, schema, warnings)]

    def _csv_rows(
        self,
        path: Path,
        dialect: type[csv.Dialect] | csv.Dialect,
        headers: list[str],
        warnings: list[str],
    ) -> Iterator[_Record]:
        """Stream CSV rows and distinguish ragged, missing, and empty cells."""
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.reader(stream, dialect)
            next(reader, None)
            for position, cells in enumerate(reader):
                keys = _column_keys(headers, len(cells))
                if len(cells) != len(headers):
                    warnings.append(
                        f"CSV row {position + 2} has {len(cells)} cells for {len(headers)} headers."
                    )
                data = dict(zip(keys, cells, strict=False))
                source = {
                    "kind": "csv",
                    "position": position,
                    "headers": headers,
                    "cells": cells,
                }
                yield _Record(data, source, [])

    def _json_resources(self, path: Path) -> list[_Resource]:
        """Split top-level lists deterministically while preserving all nested structure."""
        with path.open(encoding="utf-8") as stream:
            root = json.load(
                stream,
                parse_float=Decimal,
                parse_constant=_reject_constant,
                object_pairs_hook=_JSONObject,
            )
        warnings = _duplicate_warnings(root)
        if isinstance(root, _JSONObject) and len(root.pairs) != len(root):
            source = {"kind": "json", "position": 0, "pointer": ""}
            return [
                _Resource(
                    path.stem,
                    "object",
                    [_Record(root, source, [])],
                    self._generic_schema(),
                    warnings,
                )
            ]
        if isinstance(root, Mapping):
            lists = [(str(key), value) for key, value in root.items() if isinstance(value, list)]
            if lists:
                resources = [
                    self._json_list_resource(name, values, warnings) for name, values in lists
                ]
                metadata = {
                    key: value for key, value in root.items() if not isinstance(value, list)
                }
                if metadata:
                    name = self._unique_name("metadata", {item.name for item in resources})
                    source = {"kind": "json", "position": 0, "pointer": ""}
                    resources.append(
                        _Resource(
                            name,
                            "object",
                            [_Record(metadata, source, [])],
                            self._generic_schema(),
                            warnings,
                        )
                    )
                return resources
            source = {"kind": "json", "position": 0, "pointer": ""}
            return [
                _Resource(
                    path.stem,
                    "object",
                    [_Record(root, source, [])],
                    self._generic_schema(),
                    warnings,
                )
            ]
        if isinstance(root, list):
            return [self._json_list_resource(path.stem, root, warnings)]
        record = _Record({"value": root}, {"kind": "json", "position": 0, "pointer": ""}, [])
        return [_Resource(path.stem, "value", [record], self._generic_schema(), warnings)]

    def _json_list_resource(self, name: str, values: list[Any], warnings: list[str]) -> _Resource:
        """Wrap scalar list items only when a record object is required."""

        def records() -> Iterator[_Record]:
            """Yield list items with reconstructable positions and JSON pointers."""
            for position, value in enumerate(values):
                data = value if isinstance(value, Mapping) else {"value": value}
                pointer = f"/{name.replace('~', '~0').replace('/', '~1')}/{position}"
                yield _Record(
                    data,
                    {"kind": "json", "position": position, "pointer": pointer},
                    [],
                )

        return _Resource(name, "list", records(), self._generic_schema(), warnings)

    def _generic_schema(self) -> Document:
        """Return relationship metadata shared by non-relational resources."""
        return {"fields": [], "primary_key": [], "foreign_keys": []}

    def _unique_name(self, base: str, used: set[str]) -> str:
        """Avoid colliding with a real top-level JSON resource name."""
        name, suffix = base, 2
        while name in used:
            name, suffix = f"{base}_{suffix}", suffix + 1
        return name

    def _sqlite_resources(self, path: Path) -> list[_Resource]:
        """Inventory every non-system SQLite table and all declared relationships."""
        resources: list[_Resource] = []
        with self._sqlite_connection(path) as connection:
            tables = [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            if not tables:
                raise ValueError(f"SQLite source has no user tables: {path.name}")
            fields_by_table = {table: self._sqlite_fields(connection, table) for table in tables}
            primary_keys = {
                table: [
                    field["name"]
                    for field in sorted(fields, key=lambda item: item["primary_key_position"])
                    if field["primary_key_position"]
                ]
                for table, fields in fields_by_table.items()
            }
            for table in tables:
                fields = fields_by_table[table]
                foreign_keys = self._sqlite_foreign_keys(connection, table, primary_keys)
                primary_key = primary_keys[table]
                schema = {
                    "fields": fields,
                    "primary_key": primary_key,
                    "foreign_keys": foreign_keys,
                    "indexes": self._sqlite_indexes(connection, table),
                }
                rows = self._sqlite_rows(path, table, primary_key, foreign_keys)
                resources.append(_Resource(table, "table", rows, schema, []))
        return resources

    def _sqlite_connection(self, path: Path) -> sqlite3.Connection:
        """Open a read-only URI and return column names through Row objects."""
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _sqlite_fields(self, connection: sqlite3.Connection, table: str) -> list[Document]:
        """Reflect columns, nullability, defaults, primary-key order, and hidden state."""
        rows = connection.execute(f"PRAGMA table_xinfo({_quote(table)})")
        return [
            {
                "name": row["name"],
                "declared_type": row["type"],
                "required": bool(row["notnull"] or row["pk"]),
                "default": row["dflt_value"],
                "primary_key_position": row["pk"],
                "hidden": row["hidden"],
            }
            for row in rows
        ]

    def _sqlite_foreign_keys(
        self,
        connection: sqlite3.Connection,
        table: str,
        primary_keys: Mapping[str, list[str]],
    ) -> list[Document]:
        """Group PRAGMA rows so composite foreign keys remain one relationship."""
        groups: dict[int, Document] = {}
        for row in connection.execute(f"PRAGMA foreign_key_list({_quote(table)})"):
            relation = groups.setdefault(
                row["id"],
                {
                    "id": row["id"],
                    "target_resource": row["table"],
                    "columns": [],
                    "on_update": row["on_update"],
                    "on_delete": row["on_delete"],
                    "match": row["match"],
                },
            )
            relation["columns"].append(
                {"position": row["seq"], "local": row["from"], "target": row["to"]}
            )
        for relation in groups.values():
            target_key = primary_keys.get(relation["target_resource"], [])
            declared = [column["target"] for column in relation["columns"]]
            resolved = target_key if target_key and not any(declared) else declared
            for column, target in zip(relation["columns"], resolved, strict=False):
                column["resolved_target"] = target
            relation["targets_primary_key"] = (
                bool(target_key)
                and len(resolved) == len(target_key)
                and set(resolved) == set(target_key)
            )
        return [groups[key] for key in sorted(groups)]

    def _sqlite_indexes(self, connection: sqlite3.Connection, table: str) -> list[Document]:
        """Reflect documented index metadata without parsing SQLite DDL."""
        indexes: list[Document] = []
        for row in connection.execute(f"PRAGMA index_list({_quote(table)})"):
            columns = [
                {
                    "position": item["seqno"],
                    "column_id": item["cid"],
                    "name": item["name"],
                    "descending": bool(item["desc"]),
                    "collation": item["coll"],
                    "key": bool(item["key"]),
                }
                for item in connection.execute(f"PRAGMA index_xinfo({_quote(row['name'])})")
            ]
            indexes.append(
                {
                    "name": row["name"],
                    "unique": bool(row["unique"]),
                    "origin": row["origin"],
                    "partial": bool(row["partial"]),
                    "columns": columns,
                }
            )
        return indexes

    def _sqlite_rows(
        self,
        path: Path,
        table: str,
        primary_key: list[str],
        foreign_keys: list[Document],
    ) -> Iterator[_Record]:
        """Fetch one table in bounded batches and deterministic primary-key order."""
        order = ", ".join(_quote(column) for column in primary_key) or "rowid"
        with self._sqlite_connection(path) as connection:
            cursor = connection.execute(f"SELECT * FROM {_quote(table)} ORDER BY {order}")
            position = 0
            while rows := cursor.fetchmany(self.limits.batch_size):
                for row in rows:
                    data = dict(row)
                    source: Document = {
                        "kind": "sqlite",
                        "position": position,
                        "table": table,
                    }
                    if primary_key:
                        source["identity"] = {column: data[column] for column in primary_key}
                    relationships = self._relationships(table, data, foreign_keys)
                    yield _Record(data, source, relationships)
                    position += 1

    def _relationships(
        self,
        table: str,
        data: Mapping[str, Any],
        foreign_keys: list[Document],
    ) -> list[Document]:
        """Attach resolvable foreign-key values to each relational source record."""
        relationships: list[Document] = []
        for foreign_key in foreign_keys:
            local = {column["local"]: data[column["local"]] for column in foreign_key["columns"]}
            if any(value is None for value in local.values()):
                continue
            target = {
                column["resolved_target"]: data[column["local"]]
                for column in foreign_key["columns"]
            }
            relationships.append(
                {
                    "rel": f"{table}.foreign-key.{foreign_key['id']}",
                    "target_resource": foreign_key["target_resource"],
                    "local": local,
                    "target": target,
                    "_targets_primary_key": foreign_key["targets_primary_key"],
                }
            )
        return relationships
