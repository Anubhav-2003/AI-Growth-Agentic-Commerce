# CommerceOS

CommerceOS turns an existing CSV, JSON document, or SQLite database into two views of the same catalog:

- a lossless normalized record store that retains source fields and relationships; and
- a linked JSON website that an AI agent can browse without screenshots, selectors, or a global tool list.

A FastAPI control plane provides source setup, synchronization, mapping, inspection, and grounded chat. MongoDB stores vendor configuration, active catalog revisions, sync history, and exact source artifacts. See the [implementation plan](docs/IMPLEMENTATION_PLAN.md) for the full architecture, research record, and scope decisions.

## Prerequisites

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/)
- A reachable MongoDB server

No model key is required. Model-backed chat is optional, and catalog normalization never calls a model.

## Install and run

```bash
cp .env.example .env
uv sync --locked
uv run python main.py
```

The default local URLs are:

- Dashboard: `http://127.0.0.1:8000/`
- API documentation: `http://127.0.0.1:8000/docs`
- Health: `http://127.0.0.1:8000/api/health`
- Mongo readiness: `http://127.0.0.1:8000/api/ready`

`main.py` uses `APP_HOST` and `APP_PORT` and enables reload for local development. A deployment can start the same factory directly, for example:

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

Startup pings MongoDB and creates the required indexes. The process will not become ready if MongoDB is unavailable.

## Environment and configuration

`.env` contains deployment-specific values and secrets. It is ignored by Git. Process environment variables override it.

| Variable | Purpose | Example/default |
|---|---|---|
| `CONFIG_PATH` | Non-secret YAML configuration | `config/commerce.yml` |
| `MONGODB_URI` | MongoDB connection string | `mongodb://127.0.0.1:27017` |
| `MONGODB_DATABASE` | CommerceOS database | `agentic_commerce` |
| `SOURCE_ROOTS` | JSON list of directories sources may be read from | `["vendor_databases"]` |
| `APP_ENV` | Environment name used by the operator-key policy | `development` |
| `APP_HOST`, `APP_PORT` | Host and port used by `python main.py` | `127.0.0.1`, `8000` |
| `ADMIN_API_KEY` | Optional local, required outside configured local environments | blank locally |
| `MODEL_PROVIDER` | AnyLLM provider identifier | blank |
| `MODEL_NAME` | Provider model identifier | blank |
| `MODEL_API_BASE` | Optional provider-compatible API base | blank |

Set the provider-standard secret expected by AnyLLM, such as `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, when enabling model chat. Keys stay server-side.

[`config/commerce.yml`](config/commerce.yml) is the validated, non-secret source for route prefixes, collection names, supported extensions, batch/page/size limits, security header and local environments, mapping aliases, UCP metadata, agent-page profile, and grounded-chat copy. Do not put secrets there.

### MongoDB

CommerceOS uses the configured database and these logical collections:

- `vendor_configs`
- `catalog_resources`
- `catalog_records`
- `catalog_syncs`
- GridFS collections prefixed by `catalog_syncs_artifacts`

MongoDB may be local, containerized, or remote; only `MONGODB_URI` changes. For the example configuration, confirm connectivity with:

```bash
mongosh 'mongodb://127.0.0.1:27017' --eval 'db.runCommand({ping: 1})'
```

## Operator dashboard

Open `/` to use the single responsive control plane:

- **Overview** — published totals, readiness, current endpoint, and recent sync
- **Sources** — register CSV, JSON, or SQLite sources and start synchronization
- **Catalog** — browse resources, search records, and inspect retained JSON
- **Mapping** — review deterministic field-name suggestions and publish explicit semantics
- **Agent site** — inspect the store home, UCP discovery document, and page schema
- **Chat** — query the selected catalog with citations to exact agent record pages
- **Activity** — inspect successful and failed revision history

The old `/register`, `/login`, and `/home` URLs redirect to this dashboard. A remembered vendor ID in browser storage is only a UI selection preference; it is not authentication.

## Management API

`{reference}` accepts a vendor Mongo ID or slug. Application failures use safe RFC 9457 `application/problem+json` bodies. Starlette's request-size guard returns HTTP 413 before routing when the configured byte limit is exceeded.

| Method | Route | Behavior |
|---|---|---|
| `GET` | `/api/health` | Process health; does not assert Mongo readiness |
| `GET` | `/api/ready` | Pings MongoDB |
| `GET` | `/api/vendors` | List operator-visible storefronts |
| `POST` | `/api/vendors` | Register a source without reading it |
| `GET` | `/api/vendors/{reference}` | Vendor configuration, active stats, and last sync |
| `PATCH` | `/api/vendors/{reference}` | Update validated name, source, mapping, or public flag |
| `POST` | `/api/vendors/{reference}/sync` | Run and publish one synchronous revision |
| `GET` | `/api/vendors/{reference}/syncs` | Recent sync ledger and warnings |
| `GET` | `/api/vendors/{reference}/resources` | Active resource schemas and mapping suggestions |
| `GET` | `/api/vendors/{reference}/records` | Active records; accepts `resource`, `q`, `cursor`, and `limit` |
| `PUT` | `/api/vendors/{reference}/mapping` | Replace the explicit mapping and rebuild additive projections |
| `POST` | `/api/vendors/{reference}/chat` | Grounded answer plus record-page citations |

A minimal local workflow is:

```bash
curl -X POST http://127.0.0.1:8000/api/vendors \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Example Store",
    "slug": "example-store",
    "source": {"kind": "csv", "path": "vendor_databases/1/products.csv"},
    "public": true
  }'

curl -X POST http://127.0.0.1:8000/api/vendors/example-store/sync

curl -X PUT http://127.0.0.1:8000/api/vendors/example-store/mapping \
  -H 'Content-Type: application/json' \
  -d '{
    "mapping": {
      "resource": "products",
      "fields": {
        "id": "sku",
        "title": "title",
        "description": "description",
        "price": "price",
        "currency": "currency"
      },
      "price_units": "minor"
    }
  }'
```

Use the resource name produced by synchronization and the source's real field names. Add `-H 'X-Admin-Key: ...'` when the operator key is enabled.

## Sources, fidelity, and revisions

Supported first-release source types are:

- **CSV** — streams rows; retains original headers and positional cells, including duplicate headers, empty cells, and ragged-row warnings.
- **JSON** — preserves nested structures and decimal lexical precision; top-level lists become resources, object-held lists become separate resources, and remaining values become metadata. Duplicate members are retained through an ordered-pair wrapper and warning. JSON Lines is not accepted as plain JSON.
- **SQLite** — opens read-only, inventories every non-system table, and records columns, primary keys, foreign keys, indexes, BLOBs, and resolvable relationships.

Paths are resolved after symlinks and must remain inside `SOURCE_ROOTS`. Declared kinds must match configured extensions, and configured source/record size limits are enforced.

Each public normalized record follows this envelope:

```json
{
  "_id": "stable-sha256-id",
  "resource": "products",
  "source": {
    "kind": "csv | json | sqlite",
    "position": 0,
    "identity": {}
  },
  "data": {"every_source_field": "retained"},
  "relationships": [],
  "commerce": null,
  "sync_id": "published-revision-id"
}
```

`data` is the source-of-truth view. BSON-incompatible values receive explicit reversible wrappers—for example, base64 binary, decimal strings, dates, out-of-range integers, or ordered JSON object pairs. Search text and revision-specific physical Mongo IDs are internal; the public `_id` remains stable when the source identity is unchanged.

Synchronization hashes and retains the exact source artifact in GridFS, stages records in configured batches, rechecks the source digest, and changes `active_sync_id` only after every resource succeeds. It then removes older derived records. A failed sync removes only its staging data, records a safe failure, and leaves the previous published revision active. Synchronization currently runs in the API request rather than a background queue.

## Agent-native linked JSON

Only public vendors are reachable by store slug; legacy records without a `public` field default to public. Generic browsing works even when no commerce mapping exists.

| Method | Route | Representation |
|---|---|---|
| `GET` | `/agent/schema/page.json` | Generated Agent Page JSON Schema |
| `GET` | `/agent/{store}/` | Store home and current navigational affordances |
| `GET` | `/agent/{store}/resources` | Opaque-cursor resource collection |
| `GET` | `/agent/{store}/resources/{resource}` | Opaque-cursor record collection |
| `GET` | `/agent/{store}/resources/{resource}/schema` | Observed schema page for one resource |
| `GET` | `/agent/{store}/resources/{resource}/{record_id}` | Full lossless record and related-record links |
| `GET` | `/agent/{store}/search?q=...` | Grounded record search; optional `resource`, `cursor`, and `limit` |
| `GET` | `/agent/{store}/schema` | Store-wide observed schema page |

Pages are `application/json` with a versioned `profile` parameter and an HTTP `describedby` link. Each page contains `page`, `data`, bounded `entities`, absolute `links`, currently valid `actions`, and `meta`. Clients should follow returned URLs and treat cursors as opaque. Agent-site failures also use RFC 9457 problem documents.

Resource names that are unsafe inside one URL path segment are reversibly encoded in returned links; the record and schema payloads retain the exact source name.

## Explicit mapping and UCP

Normalization does not guess commerce meaning. Mapping suggestions use deterministic field-name similarity, and the operator approves the final mapping. Raw `data` never changes when a mapping is added or replaced; `commerce` is an additive projection.

A UCP-ready mapping explicitly supplies:

- `resource`
- fields for `id`, `title`, `description`, and `price`
- either a `currency` field or `default_currency`
- `price_units` as `major` or `minor`

Major-unit prices are converted to ISO minor units only when the result is exact; CommerceOS never guesses units from magnitude. Records missing a required mapped fact remain available through generic pages but are omitted from UCP results.

The development multi-tenant gateway implements UCP `2026-08-25` and validates documents with official `ucp-sdk` 0.5.0 models:

| Method | Route | Capability |
|---|---|---|
| `GET` | `/agent/{store}/.well-known/ucp` | Discovery; advertises only ready Search and Lookup |
| `POST` | `/agent/{store}/ucp/catalog/search` | Catalog Search with opaque cursor pagination |
| `POST` | `/agent/{store}/ucp/catalog/lookup` | Batch Lookup with partial not-found messages |
| `POST` | `/agent/{store}/ucp/catalog/product` | Single product detail under Lookup |

An unknown single-product ID is a UCP business outcome returned with HTTP 200 and `ucp.status: "error"`. Batch lookup instead returns the products it found plus informational messages for missing IDs. Malformed or unsupported transport inputs use HTTP problems. Structured UCP filters are not advertised and are currently rejected.

The path-scoped discovery URL is a development gateway convenience. A hostname-per-merchant deployment must route that merchant's root `/.well-known/ucp` to the corresponding profile before claiming root-host UCP discovery conformance.

## Chat and AnyLLM

Chat deterministically retrieves a bounded set of matching active records before answering:

- Unless both `MODEL_PROVIDER` and `MODEL_NAME` are configured, it returns a concise exact-data summary with `mode: "deterministic"`.
- With both configured, the reusable AnyLLM client sends bounded history and catalog context to the selected provider and returns `mode: "model"`.
- With no retrieved records, it returns the configured no-results message rather than inventing an answer.
- Every response includes source labels and absolute agent record URLs.

Merchant content is delimited as untrusted data, never promoted to model instructions. Provider errors become safe HTTP 502 problems. Models do not participate in source parsing, identities, schema discovery, mapping, price conversion, or revision publication.

## Operator key behavior

`ADMIN_API_KEY` is an interim operator boundary, not account or vendor authentication.

- In `development`, `test`, or `local`, a blank key leaves management routes open for local use.
- If a key is set, every management route requires the configured `X-Admin-Key` header.
- Outside configured local environments, startup fails unless a key is set.
- `/`, `/api/health`, and `/api/ready` remain reachable without the key.
- Public agent pages remain governed by the vendor's `public` flag, not the operator key.

The browser dashboard currently has no operator-key entry screen. For a protected deployment, use the management API directly with the header or place the dashboard behind a trusted same-origin proxy that authenticates the operator and supplies the header. Do not expose the local no-key mode to an untrusted network.

## Tests and checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run python -m pytest
```

Agent-web and model-layer tests are network-free. Service and management integration tests require MongoDB at the configured `MONGODB_URI`; they create uniquely named test databases and remove only those databases after the run. No provider credentials are needed.

## Project layout

```text
docs/                             Vision, plan, status, and idea documents
main.py                            Outer FastAPI factory, dashboard, and management API
config.py                          Validated environment and YAML settings
config/commerce.yml                Shared non-secret routes, limits, aliases, and protocols
models.py                          Pydantic management and agent-page contracts
services/normalization_service.py  Lossless CSV/JSON/SQLite normalization
services/catalog_service.py        Mongo persistence, revisions, queries, and projection
agent_web/app.py                   Nested linked-JSON and UCP application
model_layer/client.py              Optional AnyLLM gateway and deterministic fallback
human_ui/                          Dashboard template, styles, and browser client
tests/                             Agent, model, normalization, and integration coverage
vendor_databases/                  Local example source data
```

## Honest first-release boundaries

This release proves lossless local-source normalization, explicit projection, linked agent browsing, UCP catalog reads, operator visibility, and grounded chat. It does **not** implement:

- cart, checkout, payment, refund, return, order, or inventory mutations;
- AP2 mandates, payment credentials, or PCI-sensitive handling;
- merchant/user accounts, OAuth, sessions, or production identity authentication;
- Shopify, WooCommerce, Magento, remote SQL, object-store, webhook, or live write-back adapters;
- background jobs for long synchronizations;
- vector or embedding search;
- MCP, A2A, ACP, or autonomous browser automation;
- generative normalization or mapping.

Do not advertise or build operational workflows on those absent capabilities. Extend the shared lossless records and catalog service rather than creating a parallel source of truth.
