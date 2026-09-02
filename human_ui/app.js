"use strict";

const settings = {
  apiRoot: document.body.dataset.apiRoot,
  agentRoot: document.body.dataset.agentRoot,
  vendorKey: document.body.dataset.vendorKey,
  appName: document.body.dataset.appName,
  chatHistoryLimit: Number(document.body.dataset.chatHistoryLimit),
};
const commerceFields = [
  { name: "id", required: true },
  { name: "title", required: true },
  { name: "description", required: true },
  { name: "price", required: true },
  { name: "currency", required: true },
  { name: "category" },
  { name: "brand" },
  { name: "availability" },
  { name: "inventory" },
  { name: "image" },
  { name: "url" },
  { name: "rating" },
];
const state = {
  vendors: [], vendor: null, resources: [], records: [], syncs: [],
  resource: null, record: null, recordsCursor: null, catalogQuery: "",
  mappingResource: null, agentDocument: "home", agentJson: null,
  chat: [], chatPending: false, selectedProducts: [], cartItems: [],
  purchase: null, purchasePhase: "selected", purchaseBusy: false, syncing: false, epoch: 0,
};
const numberFormatter = new Intl.NumberFormat();
const dateFormatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

// Return an element by ID; static IDs are the UI's only DOM contract.
function byId(id) { return document.getElementById(id); }

// Create a text-safe DOM node without parsing untrusted markup.
function make(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

// Convert API collection envelopes into a predictable list.
function listFrom(data, keys = ["items", "results", "data"]) {
  if (Array.isArray(data)) return data;
  for (const key of keys) if (Array.isArray(data?.[key])) return data[key];
  return [];
}

// Read an identifier across Mongo, public, and legacy API shapes.
function idOf(item) {
  const value = item?.id ?? item?.vendor_id ?? item?._id;
  return typeof value === "object" ? String(value?.$oid || "") : String(value || "");
}

// Read the normalized source while preserving compatibility with legacy vendors.
function sourceOf(vendor) {
  return vendor?.source || { kind: vendor?.format || vendor?.type || "unknown", path: vendor?.db_path || "Not configured" };
}

// Return the resource's stable display name.
function resourceName(resource) { return String(resource?.name ?? resource?.resource ?? resource?.id ?? "resource"); }

// Read the complete raw record payload, falling back for transitional APIs.
function rawRecord(record) { return record?.data ?? record?.record ?? record ?? {}; }

// Format counts consistently while keeping unavailable values honest.
function formatCount(value) { return Number.isFinite(Number(value)) ? numberFormatter.format(Number(value)) : "—"; }

// Read publication counts from the current nested contract or legacy flat summaries.
function syncCount(sync, key) {
  if (!sync) return 0;
  if (key === "warnings" && Array.isArray(sync.warnings)) return sync.warnings.length;
  const value = sync.counts?.[key] ?? sync[key];
  return Number.isFinite(Number(value)) ? Number(value) : 0;
}

// Format timestamps defensively; malformed source dates remain visible as text.
function formatDate(value) {
  if (!value) return "Not finished";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : dateFormatter.format(date);
}

// Shorten opaque IDs for dense operational lists without altering their value.
function shortId(value) {
  const text = String(value || "unavailable");
  return text.length > 18 ? `${text.slice(0, 10)}…${text.slice(-5)}` : text;
}

// Make nested values readable in table cells without injecting HTML.
function compactValue(value) {
  if (value === null) return "null";
  if (value === undefined) return "—";
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return text.length > 86 ? `${text.slice(0, 83)}…` : text;
}

// Extract a useful error from RFC problems, FastAPI validation, or plain text.
function errorMessage(data, status) {
  const detail = data?.detail ?? data?.message ?? data?.title;
  if (Array.isArray(detail)) return detail.map((item) => item?.msg || String(item)).join(" ");
  return detail ? String(detail) : `Request failed with status ${status}.`;
}

// Fetch JSON and preserve actionable server failures for every view.
async function fetchJson(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { Accept: "application/json", ...(options.headers || {}) } });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text ? { detail: text } : null; }
  if (!response.ok) throw new Error(errorMessage(data, response.status));
  return data;
}

// Prefix one management path from the single configured API root.
function apiUrl(path = "") { return `${settings.apiRoot.replace(/\/$/, "")}${path}`; }

// Build the selected merchant's machine-site entrypoint.
function agentHome(vendor = state.vendor) {
  if (!vendor) return "";
  const store = vendor.slug || idOf(vendor);
  return `${settings.agentRoot.replace(/\/$/, "")}/${encodeURIComponent(store)}/`;
}

// Restrict rendered chat links to normal HTTP URLs.
function safeUrl(value) {
  try {
    const url = new URL(String(value), window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch { return ""; }
}

// Show a reusable loading state in an otherwise empty region.
function showLoading(container, label = "Loading…") {
  const row = make("div", "loading-line");
  row.append(make("span", "spinner"), make("span", "", label));
  container.replaceChildren(row);
}

// Show a safe empty or error state, optionally with a retry action.
function showState(container, title, detail, actionLabel, action, isError = false) {
  const box = make("div", `state-message${isError ? " is-error" : ""}`);
  box.append(make("strong", "", title), make("span", "", detail));
  if (actionLabel && action) {
    const button = make("button", "", actionLabel);
    button.type = "button";
    button.addEventListener("click", action);
    box.append(button);
  }
  container.replaceChildren(box);
}

// Surface a transient result while keeping inline view states intact.
function notify(message, kind = "success") {
  const text = String(message || "").trim();
  if (!text) return;
  const notice = byId("global-notice");
  byId("global-notice-text").textContent = text;
  notice.className = `global-notice${kind === "error" ? " is-error" : kind === "warning" ? " is-warning" : ""}`;
  notice.hidden = false;
  window.clearTimeout(notify.timer);
  notify.timer = window.setTimeout(() => { notice.hidden = true; }, 6000);
}

// Move between semantic views and load view-specific live data when needed.
function navigate(view, updateHash = true) {
  const target = document.querySelector(`[data-page="${view}"]`) ? view : "overview";
  document.querySelectorAll("[data-page]").forEach((section) => { section.hidden = section.dataset.page !== target; });
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === target;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  if (updateHash) history.replaceState(null, "", `#${target}`);
  if (target === "agent") loadAgentDocument();
  byId("main-content").focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// Resolve navigation from both sidebar and contextual buttons.
function handleNavigation(event) {
  const control = event.target.closest("[data-view], [data-go], [data-nav]");
  if (!control) return;
  event.preventDefault();
  navigate(control.dataset.view || control.dataset.go || control.dataset.nav);
}

// Reflect process health separately from catalog readiness.
async function loadHealth() {
  const pill = byId("health-pill");
  try {
    const data = await fetchJson(apiUrl("/health"));
    pill.lastChild.textContent = data?.status === "ok" ? "API online" : "API responding";
    pill.className = "health-pill is-online";
    byId("rail-signal").className = "signal is-online";
    byId("rail-status-title").textContent = "Control plane online";
    byId("rail-status-copy").textContent = "Management API is responding";
  } catch (error) {
    pill.lastChild.textContent = "API unavailable";
    pill.className = "health-pill is-offline";
    byId("rail-signal").className = "signal is-offline";
    byId("rail-status-title").textContent = "Control plane offline";
    byId("rail-status-copy").textContent = error.message;
  }
}

// Populate the storefront selector without treating browser storage as auth.
function renderVendorSelect() {
  const select = byId("vendor-select");
  select.replaceChildren();
  if (!state.vendors.length) {
    const option = make("option", "", "No storefronts connected");
    option.value = "";
    select.append(option);
    select.disabled = true;
    return;
  }
  for (const vendor of state.vendors) {
    const option = make("option", "", vendor.name || vendor.slug || shortId(idOf(vendor)));
    option.value = idOf(vendor);
    option.selected = idOf(vendor) === idOf(state.vendor);
    select.append(option);
  }
  select.disabled = false;
}

// Load vendors, honoring only a remembered selection preference.
async function loadVendors(preferredId = localStorage.getItem(settings.vendorKey)) {
  try {
    const data = await fetchJson(apiUrl("/vendors"));
    state.vendors = listFrom(data, ["vendors", "items", "data"]);
    const selected = state.vendors.find((vendor) => idOf(vendor) === preferredId) || state.vendors[0] || null;
    await selectVendor(selected);
  } catch (error) {
    state.vendors = [];
    await selectVendor(null);
    notify(`Storefronts could not be loaded: ${error.message}`, "error");
  }
}

// Change the shared merchant context and discard stale per-store state.
async function selectVendor(vendor) {
  state.vendor = vendor;
  state.resources = [];
  state.records = [];
  state.syncs = [];
  state.resource = null;
  state.record = null;
  state.mappingResource = null;
  state.agentJson = null;
  state.chat = [];
  state.selectedProducts = [];
  state.cartItems = [];
  state.purchase = null;
  state.purchasePhase = "selected";
  state.purchaseBusy = false;
  state.epoch += 1;
  if (vendor) localStorage.setItem(settings.vendorKey, idOf(vendor)); else localStorage.removeItem(settings.vendorKey);
  renderVendorSelect();
  renderAll();
  if (vendor) await loadVendorBundle(state.epoch);
}

// Fetch vendor details, resources, and revision history in parallel.
async function loadVendorBundle(epoch = state.epoch) {
  if (typeof epoch !== "number") epoch = state.epoch;
  const id = idOf(state.vendor);
  showLoading(byId("resource-list"), "Loading resources…");
  showLoading(byId("recent-sync"), "Loading publication history…");
  showLoading(byId("activity-list"), "Loading revisions…");
  const requests = await Promise.allSettled([
    fetchJson(apiUrl(`/vendors/${encodeURIComponent(id)}`)),
    fetchJson(apiUrl(`/vendors/${encodeURIComponent(id)}/resources`)),
    fetchJson(apiUrl(`/vendors/${encodeURIComponent(id)}/syncs`)),
  ]);
  if (epoch !== state.epoch) return;
  if (requests[0].status === "fulfilled") {
    const detail = requests[0].value?.vendor || requests[0].value;
    if (detail && typeof detail === "object") {
      state.vendor = detail;
      const index = state.vendors.findIndex((vendor) => idOf(vendor) === id);
      if (index >= 0) state.vendors[index] = detail;
    }
  }
  state.resources = requests[1].status === "fulfilled" ? listFrom(requests[1].value, ["resources", "items", "data"]) : [];
  state.syncs = requests[2].status === "fulfilled" ? listFrom(requests[2].value, ["syncs", "items", "data"]) : [];
  state.resource = state.resources[0] || null;
  state.mappingResource = state.vendor?.mapping?.resource || (state.resource && resourceName(state.resource));
  renderAll();
  if (requests[1].status === "rejected") showState(byId("resource-list"), "Resources unavailable", requests[1].reason.message, "Retry", loadVendorBundle, true);
  if (requests[2].status === "rejected") {
    showState(byId("activity-list"), "History unavailable", requests[2].reason.message, "Retry", loadVendorBundle, true);
    showState(byId("recent-sync"), "History unavailable", requests[2].reason.message, "Retry", loadVendorBundle, true);
  }
  if (state.resource) await loadRecords();
}

// Derive projection readiness from explicit mapping facts only.
function readiness() {
  const mapping = state.vendor?.mapping || {};
  const fields = mapping.fields || {};
  const checks = [
    { label: "At least one normalized resource is published", done: state.resources.length > 0 },
    { label: "A product resource is explicitly selected", done: Boolean(mapping.resource) },
    { label: "Stable product identity is mapped", done: Boolean(fields.id) },
    { label: "Product title is mapped", done: Boolean(fields.title) },
    { label: "Product description is mapped", done: Boolean(fields.description) },
    { label: "Price field and units are asserted", done: Boolean(fields.price && mapping.price_units) },
    { label: "Currency is mapped or explicitly defaulted", done: Boolean(fields.currency || mapping.default_currency) },
  ];
  const complete = checks.filter((check) => check.done).length;
  return { checks, percent: Math.round((complete / checks.length) * 100), ready: complete === checks.length };
}

// Return the most recent revision regardless of server ordering.
function latestSync() {
  return [...state.syncs].sort((a, b) => new Date(b.finished_at || b.started_at || 0) - new Date(a.finished_at || a.started_at || 0))[0] || null;
}

// Update overview metrics, readiness, and machine entrypoint.
function renderOverview() {
  const vendor = state.vendor;
  const ready = readiness();
  const recent = latestSync();
  const records = state.resources.reduce((sum, resource) => sum + Number(resource.record_count || resource.records || 0), 0) || syncCount(recent, "records");
  const resources = state.resources.length || syncCount(recent, "resources");
  byId("overview-summary").textContent = vendor ? `${vendor.name || "This storefront"} is preserved as linked machine pages for direct agent navigation.` : "Connect a source, preserve every field, and publish a traversable machine-first catalog.";
  byId("metric-readiness").textContent = vendor ? `${ready.percent}%` : "—";
  byId("metric-readiness-copy").textContent = ready.ready ? "UCP catalog projection ready" : vendor ? "Mapping review required" : "Waiting for a storefront";
  byId("metric-records").textContent = vendor ? formatCount(records || 0) : "—";
  byId("metric-resources").textContent = vendor ? formatCount(resources) : "—";
  byId("metric-sync").textContent = recent ? formatDate(recent.finished_at || recent.started_at).split(",")[0] : "—";
  byId("metric-sync-copy").textContent = recent ? String(recent.status || "Recorded revision") : "No sync loaded";
  byId("readiness-chip").textContent = ready.ready ? "Projection ready" : vendor ? `${ready.percent}% ready` : "Not assessed";
  byId("readiness-chip").className = `status-chip ${ready.ready ? "is-success" : vendor ? "is-warning" : ""}`;
  byId("readiness-bar").style.width = `${vendor ? ready.percent : 0}%`;
  byId("readiness-copy").textContent = ready.ready ? "Required identity, title, description, price, and currency facts are approved." : "Approve deterministic field mappings before advertising UCP catalog capabilities.";
  renderChecks(byId("readiness-list"), ready.checks);
  const url = agentHome();
  byId("overview-agent-url").textContent = url ? new URL(url, location.origin).href : "No storefront selected";
  byId("copy-overview-url").disabled = !url;
  for (const button of [byId("overview-sync"), byId("source-sync"), byId("activity-sync")]) button.disabled = !vendor || state.syncing;
  renderRecentSync();
}

// Render a uniform list of completed and outstanding assertions.
function renderChecks(container, checks) {
  container.replaceChildren();
  for (const check of checks) {
    const item = make("li", check.done ? "is-complete" : "is-warning", check.label);
    container.append(item);
  }
}

// Summarize the newest revision without hiding warnings or failures.
function renderRecentSync() {
  const container = byId("recent-sync");
  const sync = latestSync();
  if (!state.vendor) return showState(container, "No storefront selected", "Connect a source to create a publication ledger.");
  if (!sync) return showState(container, "No revisions yet", "Run the first sync to publish normalized resources.");
  const row = make("div", "sync-preview");
  row.append(make("span", "", String(sync.status || "").toLowerCase().includes("fail") ? "!" : "↻"));
  const identity = make("div");
  identity.append(make("h3", "", `${String(sync.status || "Revision")} · ${shortId(sync.sync_id || sync.id)}`), make("p", "", formatDate(sync.finished_at || sync.started_at)));
  row.append(identity);
  for (const [label, value] of [["Resources", syncCount(sync, "resources")], ["Records", syncCount(sync, "records")], ["Warnings", syncCount(sync, "warnings")]]) {
    const stat = make("div", "preview-stat");
    stat.append(make("small", "", label), make("strong", "", formatCount(value || 0)));
    row.append(stat);
  }
  container.replaceChildren(row);
}

// Display the selected source and its safe operational metadata.
function renderSource() {
  const container = byId("source-summary");
  const chip = byId("source-status");
  if (!state.vendor) {
    chip.textContent = "No source";
    chip.className = "status-chip";
    return showState(container, "Nothing connected", "Use the form to register a CSV, JSON, or SQLite source.");
  }
  const source = sourceOf(state.vendor);
  chip.textContent = state.vendor.status || "Configured";
  chip.className = `status-chip ${String(state.vendor.status || "").includes("error") ? "is-error" : "is-success"}`;
  const card = make("div", "source-card");
  card.append(make("span", "source-badge", String(source.kind || "data").toUpperCase()), make("h3", "", state.vendor.name || "Unnamed storefront"), make("p", "", source.path));
  const list = make("dl", "definition-list");
  for (const [term, value] of [["Store ID", shortId(idOf(state.vendor))], ["Visibility", state.vendor.public === false ? "Private" : "Public"], ["Resources", state.resources.length || syncCount(latestSync(), "resources")], ["Last sync", latestSync() ? formatDate(latestSync().finished_at || latestSync().started_at) : "Never"]]) {
    const row = make("div");
    row.append(make("dt", "", term), make("dd", "", value));
    list.append(row);
  }
  card.append(list);
  container.replaceChildren(card);
}

// Render resource choices and preserve selection through data attributes.
function renderResources() {
  const container = byId("resource-list");
  byId("resource-count").textContent = formatCount(state.resources.length);
  if (!state.vendor) return showState(container, "Select a storefront", "Resources belong to the active storefront.");
  if (!state.resources.length) return showState(container, "No resources published", "Synchronize the source to inventory its tables or collections.", "Sync now", syncVendor);
  container.replaceChildren();
  for (const resource of state.resources) {
    const button = make("button", `resource-option${resource === state.resource ? " is-selected" : ""}`);
    button.type = "button";
    button.dataset.resource = resourceName(resource);
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(resource === state.resource));
    const label = make("span");
    label.append(make("strong", "", resourceName(resource)), make("small", "", resource.kind || "resource"));
    button.append(label, make("span", "", formatCount(resource.record_count || resource.records || 0)));
    container.append(button);
  }
}

// Select a resource through event delegation and load its records.
function handleResourceClick(event) {
  const button = event.target.closest("[data-resource]");
  if (!button) return;
  state.resource = state.resources.find((resource) => resourceName(resource) === button.dataset.resource) || null;
  state.record = null;
  state.records = [];
  renderResources();
  renderRecordInspector();
  loadRecords();
}

// Fetch one bounded page of records for the active resource and query.
async function loadRecords(cursor = "", append = false) {
  if (typeof cursor !== "string") cursor = "";
  if (!state.vendor || !state.resource) return renderRecords();
  const epoch = state.epoch;
  if (!append) showLoading(byId("records-state"), "Loading records…");
  const params = new URLSearchParams({ resource: resourceName(state.resource), limit: "50" });
  if (state.catalogQuery) params.set("q", state.catalogQuery);
  if (cursor) params.set("cursor", cursor);
  try {
    const data = await fetchJson(apiUrl(`/vendors/${encodeURIComponent(idOf(state.vendor))}/records?${params}`));
    if (epoch !== state.epoch) return;
    const records = listFrom(data, ["records", "items", "results", "data"]);
    state.records = append ? state.records.concat(records) : records;
    state.recordsCursor = data?.next_cursor ?? data?.pagination?.next_cursor ?? data?.meta?.next_cursor ?? null;
    renderRecords();
  } catch (error) {
    showState(byId("records-state"), "Records unavailable", error.message, "Retry", loadRecords, true);
  }
}

// Choose representative columns from observed schema or the first record.
function recordColumns() {
  const fields = schemaFields(state.resource);
  const raw = rawRecord(state.records[0]);
  const available = fields.length ? fields : Object.keys(raw || {});
  const preferred = ["id", "sku", "asin", "parent_asin", "title", "name", "price", "category"];
  const ordered = [...preferred.filter((name) => available.includes(name)), ...available.filter((name) => !preferred.includes(name))];
  return [...new Set(ordered)].slice(0, 3);
}

// Paint the record table with buttons so rows remain keyboard-operable.
function renderRecords() {
  const head = byId("records-head");
  const body = byId("records-body");
  const stateBox = byId("records-state");
  const columns = recordColumns();
  byId("records-title").textContent = state.resource ? resourceName(state.resource) : "Records";
  byId("records-caption").textContent = state.resource ? `${state.catalogQuery ? "Filtered" : "Published"} records from this source resource.` : "Choose a resource to begin.";
  byId("record-count").textContent = formatCount(state.records.length);
  head.replaceChildren();
  body.replaceChildren();
  if (!state.resource) return showState(stateBox, "Choose a resource", "Select a source collection to inspect its records.");
  if (!state.records.length) return showState(stateBox, state.catalogQuery ? "No matching records" : "No records", state.catalogQuery ? "Try a broader search term." : "The selected resource is currently empty.");
  const headerRow = make("tr");
  for (const column of columns) headerRow.append(make("th", "", column));
  head.append(headerRow);
  state.records.forEach((record, index) => {
    const row = make("tr");
    const cell = make("td");
    cell.colSpan = Math.max(columns.length, 1);
    const button = make("button", `record-button${record === state.record ? " is-selected" : ""}`);
    button.type = "button";
    button.dataset.recordIndex = String(index);
    button.style.setProperty("--column-count", String(Math.max(columns.length, 1)));
    const raw = rawRecord(record);
    for (const column of columns.length ? columns : ["record"]) button.append(make("span", "record-cell", compactValue(column === "record" ? raw : raw?.[column])));
    cell.append(button);
    row.append(cell);
    body.append(row);
  });
  stateBox.replaceChildren();
  if (state.recordsCursor) {
    const more = make("button", "button button-secondary", "Load more records");
    more.type = "button";
    more.addEventListener("click", loadMoreRecords);
    stateBox.append(more);
  }
}

// Continue opaque-cursor pagination without replacing inspected records.
function loadMoreRecords() { loadRecords(state.recordsCursor, true); }

// Select a record through table event delegation.
function handleRecordClick(event) {
  const button = event.target.closest("[data-record-index]");
  if (!button) return;
  state.record = state.records[Number(button.dataset.recordIndex)] || null;
  renderRecords();
  renderRecordInspector();
}

// Show complete JSON through textContent and enable safe copying.
function renderRecordInspector() {
  byId("record-json").textContent = state.record ? JSON.stringify(state.record, null, 2) : "Select a record to inspect every retained field.";
  byId("copy-record").disabled = !state.record;
}

// Return field names from observed schema variants.
function schemaFields(resource) {
  const fields = resource?.schema?.fields || resource?.fields || [];
  if (Array.isArray(fields) && fields.length) return [...new Set(fields.map((field) => typeof field === "string" ? field : field?.storage_name || field?.name).filter(Boolean))];
  return Object.keys(resource?.schema?.observed?.properties || resource?.schema?.properties || {});
}

// Read one deterministic mapping suggestion and confidence score.
function mappingSuggestion(resource, target) {
  const value = (resource?.mapping_suggestions || resource?.suggestions || {})[target];
  if (typeof value === "string") return { field: value, score: null, selected: true };
  return {
    field: value?.field || value?.source || value?.name || "",
    score: value?.score ?? value?.confidence ?? null,
    selected: value?.selected !== false && !value?.ambiguous,
  };
}

// Build the mapping editor from the selected resource's observed fields.
function renderMapping() {
  const resourceSelect = byId("mapping-resource");
  const fieldContainer = byId("mapping-fields");
  resourceSelect.replaceChildren();
  if (!state.resources.length) {
    resourceSelect.append(Object.assign(make("option", "", "No resources available"), { value: "" }));
    resourceSelect.disabled = true;
    byId("mapping-submit").disabled = true;
    showState(fieldContainer, "Sync before mapping", "Observed source fields appear here after a successful sync.");
    updateMappingReadiness();
    return;
  }
  for (const resource of state.resources) {
    const name = resourceName(resource);
    const option = make("option", "", name);
    option.value = name;
    option.selected = name === (state.mappingResource || resourceName(state.resources[0]));
    resourceSelect.append(option);
  }
  resourceSelect.disabled = false;
  byId("mapping-submit").disabled = false;
  state.mappingResource = resourceSelect.value;
  const resource = state.resources.find((item) => resourceName(item) === state.mappingResource);
  const fields = schemaFields(resource);
  const mapping = state.vendor?.mapping?.resource === state.mappingResource ? state.vendor.mapping : {};
  byId("price-units").value = mapping.price_units || "";
  byId("default-currency").value = mapping.default_currency || "";
  fieldContainer.replaceChildren();
  for (const target of commerceFields) {
    const row = make("div", "mapping-row");
    const label = make("div", "mapping-label");
    label.append(make("strong", "", target.name));
    if (target.required) label.append(make("span", "field-required", "required"));
    const suggestion = mappingSuggestion(resource, target.name);
    if (suggestion.score !== null) label.append(make("span", "suggestion-score", `${Math.round(Number(suggestion.score))}% match`));
    const select = document.createElement("select");
    select.name = `field_${target.name}`;
    select.dataset.target = target.name;
    select.setAttribute("aria-label", `Source field for ${target.name}`);
    const blank = make("option", "", "Not mapped");
    blank.value = "";
    select.append(blank);
    for (const field of fields) {
      const option = make("option", "", field);
      option.value = field;
      const selected = mapping.fields?.[target.name] || (suggestion.selected ? suggestion.field : "");
      option.selected = field === selected;
      select.append(option);
    }
    select.addEventListener("change", updateMappingReadiness);
    row.append(label, select);
    fieldContainer.append(row);
  }
  updateMappingReadiness();
}

// Recalculate mapping readiness from visible explicit form choices.
function updateMappingReadiness() {
  const values = Object.fromEntries([...document.querySelectorAll("#mapping-fields select[data-target]")].map((select) => [select.dataset.target, select.value]));
  const checks = [
    { label: "Product resource selected", done: Boolean(byId("mapping-resource").value) },
    { label: "Identity field selected", done: Boolean(values.id) },
    { label: "Title field selected", done: Boolean(values.title) },
    { label: "Description field selected", done: Boolean(values.description) },
    { label: "Price units declared", done: Boolean(values.price && byId("price-units").value) },
    { label: "Currency mapped or explicitly defaulted", done: Boolean(values.currency || byId("default-currency").value.trim()) },
  ];
  const percent = Math.round((checks.filter((check) => check.done).length / checks.length) * 100);
  byId("mapping-score").textContent = `${percent}%`;
  renderChecks(byId("mapping-checks"), checks);
}

// Publish the merchant-approved projection as one versioned mapping.
async function saveMapping(event) {
  event.preventDefault();
  if (!state.vendor || !byId("mapping-form").reportValidity()) return;
  const button = byId("mapping-submit");
  const status = byId("mapping-status");
  const fields = {};
  for (const select of document.querySelectorAll("#mapping-fields select[data-target]")) if (select.value) fields[select.dataset.target] = select.value;
  const mapping = {
    resource: byId("mapping-resource").value,
    fields,
    price_units: byId("price-units").value || null,
    default_currency: byId("default-currency").value.trim().toUpperCase() || null,
  };
  button.disabled = true;
  button.textContent = "Publishing…";
  status.className = "form-status";
  status.textContent = "Validating explicit field semantics…";
  try {
    await fetchJson(apiUrl(`/vendors/${encodeURIComponent(idOf(state.vendor))}/mapping`), { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mapping }) });
    state.vendor.mapping = mapping;
    status.className = "form-status is-success";
    status.textContent = "Mapping published. Raw source records remain unchanged.";
    notify("Commerce mapping published.");
    renderOverview();
  } catch (error) {
    status.className = "form-status is-error";
    status.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Publish mapping →";
  }
}

// Register a new source using the nested management contract.
async function registerVendor(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const data = new FormData(form);
  const payload = {
    name: String(data.get("name") || "").trim(),
    source: { kind: String(data.get("kind") || ""), path: String(data.get("path") || "").trim() },
    public: data.get("public") === "on",
  };
  const slug = String(data.get("slug") || "").trim();
  if (slug) payload.slug = slug;
  const button = byId("vendor-submit");
  const status = byId("vendor-form-status");
  button.disabled = true;
  button.textContent = "Connecting…";
  status.className = "form-status";
  status.textContent = "Validating source configuration…";
  try {
    const result = await fetchJson(apiUrl("/vendors"), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const preferred = idOf(result?.vendor || result) || result?.vendor_id;
    form.reset();
    status.className = "form-status is-success";
    status.textContent = "Storefront connected. You can synchronize it now.";
    notify("Storefront connected successfully.");
    await loadVendors(preferred);
  } catch (error) {
    status.className = "form-status is-error";
    status.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "Connect storefront →";
  }
}

// Disable every sync entrypoint while one publication is in flight.
function setSyncBusy(busy) {
  state.syncing = busy;
  for (const button of [byId("overview-sync"), byId("source-sync"), byId("activity-sync")]) {
    button.disabled = busy || !state.vendor;
    button.textContent = busy ? "Synchronizing…" : button.id === "source-sync" ? "↻ Sync selected" : button.id === "activity-sync" ? "↻ Start sync" : "↻ Sync catalog";
  }
}

// Publish a complete normalization revision and refresh all affected views.
async function syncVendor() {
  if (!state.vendor || state.syncing) return;
  setSyncBusy(true);
  try {
    const result = await fetchJson(apiUrl(`/vendors/${encodeURIComponent(idOf(state.vendor))}/sync`), { method: "POST" });
    notify(result?.message || "Catalog synchronization completed.");
    await loadVendorBundle(state.epoch);
  } catch (error) {
    notify(`Synchronization failed: ${error.message}`, "error");
  } finally { setSyncBusy(false); }
}

// Render the immutable revision ledger in a scannable operational table.
function renderActivity() {
  const container = byId("activity-list");
  if (!state.vendor) return showState(container, "No storefront selected", "Choose a storefront to inspect its revisions.");
  if (!state.syncs.length) return showState(container, "No sync history", "The first complete publication will appear here.", "Start first sync", syncVendor);
  container.replaceChildren();
  for (const sync of state.syncs) {
    const status = String(sync.status || "unknown").toLowerCase();
    const row = make("div", "activity-row");
    const revision = make("div", "activity-revision");
    revision.append(make("strong", "", shortId(sync.sync_id || sync.id)), make("small", "", formatDate(sync.started_at)));
    row.append(revision, make("span", `sync-status ${status.includes("success") || status.includes("complete") || status === "published" ? "is-success" : status.includes("fail") || status === "error" ? "is-error" : "is-running"}`, status));
    row.append(make("span", "activity-cell", formatCount(syncCount(sync, "resources"))), make("span", "activity-cell", formatCount(syncCount(sync, "records"))), make("span", "activity-cell", formatDate(sync.finished_at)));
    container.append(row);
  }
}

// Resolve the selected live agent document URL.
function agentDocumentUrl() {
  const home = agentHome();
  if (!home) return "";
  if (state.agentDocument === "ucp") return `${home}.well-known/ucp`;
  if (state.agentDocument === "schema") return `${home}schema`;
  return home;
}

// Fetch and display the exact machine response without interpreting its content.
async function loadAgentDocument() {
  const url = agentDocumentUrl();
  const status = byId("agent-fetch-status");
  byId("agent-document-url").textContent = url || "Select a storefront";
  const open = byId("open-agent");
  open.href = url || "#";
  open.classList.toggle("is-disabled", !url);
  if (!url) {
    state.agentJson = null;
    byId("agent-json").textContent = "Connect or select a storefront to inspect its machine site.";
    byId("copy-agent-json").disabled = true;
    status.textContent = "Not loaded";
    return;
  }
  status.textContent = "Fetching…";
  byId("agent-json").textContent = "Loading the live machine page…";
  try {
    state.agentJson = await fetchJson(url);
    byId("agent-json").textContent = JSON.stringify(state.agentJson, null, 2);
    byId("copy-agent-json").disabled = false;
    status.textContent = "Live response";
  } catch (error) {
    state.agentJson = null;
    byId("agent-json").textContent = `Unable to load this document.\n\n${error.message}`;
    byId("copy-agent-json").disabled = true;
    status.textContent = "Request failed";
  }
}

// Switch inspector tabs and keep ARIA selection synchronized.
function chooseAgentDocument(event) {
  const button = event.target.closest("[data-document]");
  if (!button) return;
  state.agentDocument = button.dataset.document;
  document.querySelectorAll("[data-document]").forEach((tab) => tab.setAttribute("aria-selected", String(tab === button)));
  loadAgentDocument();
}

// Render an initial chat prompt or the complete safe conversation log.
function sourceTitle(source) {
  const title = String(source?.title || "").trim();
  const label = String(source?.label || "").trim();
  const opaque = /^[\w-]+\/[a-f0-9]{8,}/i;
  if (title && !opaque.test(title)) return title;
  if (label && !opaque.test(label)) return label;
  return "View product";
}

function productKey(product) {
  return String(product?.record_id || "").trim();
}

function selectedEntry(product) {
  const key = productKey(product);
  return key ? state.selectedProducts.find((item) => productKey(item) === key) : null;
}

function formatAmount(amount, currency) {
  const value = Number(amount);
  const code = String(currency || "USD").trim() || "USD";
  if (!Number.isFinite(value)) return "";
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: code }).format(value);
  } catch {
    return `${value} ${code}`;
  }
}

function formatMoney(product) {
  return formatAmount(product?.price, product?.currency);
}

function lineAmount(item) {
  const price = Number(item?.price ?? item?.unit_price);
  const quantity = quantityValue(item);
  if (!Number.isFinite(price)) return null;
  return (Math.round(price * 100) * quantity) / 100;
}

function selectionTotal(items) {
  return items.reduce((sum, item) => sum + (lineAmount(item) || 0), 0);
}

function looksLikeBuyIntent(text) {
  return /\b(buy|purchase|check\s*out|pay for)\b/i.test(String(text || ""));
}

function knownStock(product) {
  const count = Number(product?.inventory);
  return Number.isFinite(count) && count >= 0 ? count : null;
}

function stockLabel(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const labels = { in_stock: "In stock", low_stock: "Low stock", out_of_stock: "Out of stock" };
  return labels[raw] || raw.replaceAll("_", " ");
}

function stockMessage(product) {
  const count = knownStock(product);
  if (count !== null) return `${numberFormatter.format(count)} available`;
  return stockLabel(product.availability);
}

function quantityValue(item) {
  const count = Math.trunc(Number(item?.quantity));
  return Number.isFinite(count) && count >= 1 ? count : 1;
}

function quantityNote(item) {
  const stock = knownStock(item);
  const quantity = quantityValue(item);
  if (stock !== null && quantity > stock) return `Only ${numberFormatter.format(stock)} are currently shown as available.`;
  return "";
}

function appendQuantityControls(card, item) {
  const row = make("div", "product-qty");
  const quantity = quantityValue(item);
  const minus = make("button", "icon-button product-qty-btn", "−");
  minus.type = "button";
  minus.dataset.quantityDelta = "-1";
  minus.dataset.productKey = productKey(item);
  minus.setAttribute("aria-label", "Decrease quantity");
  minus.disabled = quantity <= 1;
  const plus = make("button", "icon-button product-qty-btn", "+");
  plus.type = "button";
  plus.dataset.quantityDelta = "1";
  plus.dataset.productKey = productKey(item);
  plus.setAttribute("aria-label", "Increase quantity");
  const value = make("span", "product-qty-value", String(quantity));
  value.setAttribute("aria-live", "polite");
  row.append(make("span", "product-qty-label", "Quantity"), minus, value, plus);
  card.append(row);
  const note = quantityNote(item);
  if (note) card.append(make("span", "product-qty-note", note));
}

function appendProductCard(container, source, messageIndex, sourceIndex) {
  const product = source.product;
  const selected = selectedEntry(product);
  const card = make("article", `product-pick${selected ? " is-selected" : ""}`);
  card.append(make("strong", "product-pick-name", product.name || sourceTitle(source)));
  if (product.brand) card.append(make("span", "product-pick-brand", product.brand));
  const price = formatMoney(product);
  if (price) card.append(make("span", "product-pick-price", price));
  const stock = stockMessage(product);
  if (stock) card.append(make("span", "product-pick-stock", stock));
  if (selected) appendQuantityControls(card, selected);
  const actions = make("div", "product-pick-actions");
  const button = make("button", `button ${selected ? "button-secondary" : "button-primary"} product-pick-select`);
  button.type = "button";
  button.dataset.selectProduct = "true";
  button.dataset.message = String(messageIndex);
  button.dataset.source = String(sourceIndex);
  button.setAttribute("aria-pressed", String(Boolean(selected)));
  button.textContent = selected ? "Selected ✓" : "Select product";
  actions.append(button);
  if (selected) {
    const remove = make("button", "button button-quiet product-pick-remove", "Remove");
    remove.type = "button";
    remove.dataset.removeProduct = productKey(selected);
    actions.append(remove);
  }
  card.append(actions);
  container.append(card);
}

function selectProduct(product) {
  const key = productKey(product);
  if (!key) return;
  if (selectedEntry(product)) {
    renderChat();
    return;
  }
  state.selectedProducts.push({ ...product, quantity: 1 });
  state.chat.push({
    role: "assistant",
    content: `Selected ${product.name || "this product"}.`,
    mode: "selection",
    local: true,
  });
  renderChat();
}

function changeQuantity(key, delta) {
  const item = state.selectedProducts.find((entry) => productKey(entry) === String(key || ""));
  if (!item) return;
  const next = Math.max(1, quantityValue(item) + (Math.trunc(Number(delta)) || 0));
  if (next === quantityValue(item)) return;
  item.quantity = next;
  renderChat();
}

function removeSelectedProduct(key) {
  const identity = String(key || "");
  const remaining = state.selectedProducts.filter((item) => productKey(item) !== identity);
  if (remaining.length === state.selectedProducts.length) return;
  state.selectedProducts = remaining;
  renderChat();
}

function addToCart() {
  if (!state.selectedProducts.length) return;
  state.cartItems = state.selectedProducts.map((item) => ({ ...item }));
  state.chat.push({
    role: "assistant",
    content: "Added to cart. No payment has been made, and availability has not been reserved.",
    mode: "selection",
    local: true,
  });
  renderChat();
}

function purchaseLines() {
  return state.selectedProducts.map((item) => ({
    record_id: productKey(item),
    quantity: quantityValue(item),
    displayed_price: Number.isFinite(Number(item.price)) ? Number(item.price) : undefined,
  }));
}

async function reviewPurchase() {
  if (!state.vendor || !state.selectedProducts.length || state.purchaseBusy) return;
  state.purchaseBusy = true;
  renderChat();
  try {
    const result = await fetchJson(
      apiUrl(`/vendors/${encodeURIComponent(idOf(state.vendor))}/purchases/review`),
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: purchaseLines() }) }
    );
    state.purchase = result?.purchase || null;
    state.purchasePhase = "review";
  } catch (error) {
    notify(error.message, "error");
    state.chat.push({ role: "assistant", content: error.message, mode: "error", local: true });
  } finally {
    state.purchaseBusy = false;
    renderChat();
  }
}

async function confirmPurchase() {
  if (!state.vendor || !state.purchase?.id || state.purchaseBusy) return;
  state.purchaseBusy = true;
  renderChat();
  try {
    const result = await fetchJson(
      apiUrl(`/vendors/${encodeURIComponent(idOf(state.vendor))}/purchases/${encodeURIComponent(state.purchase.id)}/authorize`),
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) }
    );
    state.purchase = result?.purchase || state.purchase;
    state.purchasePhase = "authorized";
    state.chat.push({
      role: "assistant",
      content: result?.message || "Your purchase is authorized. Payment has not started yet.",
      mode: "selection",
      local: true,
    });
  } catch (error) {
    notify(error.message, "error");
    state.chat.push({ role: "assistant", content: error.message, mode: "error", local: true });
  } finally {
    state.purchaseBusy = false;
    renderChat();
  }
}

async function cancelPurchase() {
  const attempt = state.purchase?.id;
  if (state.purchaseBusy) return;
  if (!attempt) {
    state.purchase = null;
    state.purchasePhase = "cancelled";
    renderChat();
    return;
  }
  state.purchaseBusy = true;
  renderChat();
  try {
    const result = await fetchJson(
      apiUrl(`/vendors/${encodeURIComponent(idOf(state.vendor))}/purchases/${encodeURIComponent(attempt)}/cancel`),
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }
    );
    state.purchase = result?.purchase || state.purchase;
    state.purchasePhase = "cancelled";
    state.chat.push({
      role: "assistant",
      content: result?.message || "Purchase cancelled. Inventory was not changed.",
      mode: "selection",
      local: true,
    });
  } catch (error) {
    notify(error.message, "error");
  } finally {
    state.purchaseBusy = false;
    renderChat();
  }
}

function appendSummaryCard(container, item) {
  const card = make("article", "purchase-line");
  card.append(make("strong", "purchase-line-name", item.name || "Product"));
  if (item.brand) card.append(make("span", "purchase-line-brand", item.brand));
  card.append(make("span", "purchase-line-qty", `Quantity: ${quantityValue(item)}`));
  const unit = formatAmount(item.unit_price ?? item.price, item.currency);
  if (unit) card.append(make("span", "purchase-line-price", `${unit} each`));
  const subtotal = formatAmount(item.subtotal ?? lineAmount(item), item.currency);
  if (subtotal) card.append(make("span", "purchase-line-subtotal", `Subtotal: ${subtotal}`));
  container.append(card);
}

function handleProductSelect(event) {
  const control = event.target.closest("[data-select-product], [data-quantity-delta], [data-remove-product], [data-add-to-cart], [data-buy-now], [data-review-purchase], [data-confirm-purchase], [data-cancel-purchase]");
  if (!control) return;
  event.preventDefault();
  if (control.dataset.selectProduct) {
    const message = state.chat[Number(control.dataset.message)];
    const source = message?.sources?.[Number(control.dataset.source)];
    if (source?.product) selectProduct(source.product);
    return;
  }
  if (control.dataset.quantityDelta) {
    changeQuantity(control.dataset.productKey, control.dataset.quantityDelta);
    return;
  }
  if (control.dataset.removeProduct) {
    removeSelectedProduct(control.dataset.removeProduct);
    return;
  }
  if (control.dataset.addToCart) {
    addToCart();
    return;
  }
  if (control.dataset.buyNow || control.dataset.reviewPurchase) {
    reviewPurchase();
    return;
  }
  if (control.dataset.confirmPurchase) {
    confirmPurchase();
    return;
  }
  if (control.dataset.cancelPurchase) cancelPurchase();
}

function renderChat() {
  const log = byId("chat-log");
  const suggestions = byId("prompt-suggestions");
  log.replaceChildren();
  suggestions.hidden = Boolean(state.chat.length || state.chatPending);
  if (!state.chat.length && !state.chatPending) {
    const empty = make("div", "chat-empty");
    const store = state.vendor?.name || "this store";
    empty.append(
      make("span", "chat-glyph", "✦"),
      make("h2", "", state.vendor ? `Ask about ${store}` : "Select a storefront first"),
      make("p", "", state.vendor ? `Ask about products, prices, or recommendations. ${settings.appName} will browse the store and answer from what it finds.` : "Choose a storefront to start a conversation.")
    );
    log.append(empty);
  }
  state.chat.forEach((message, messageIndex) => {
    const item = make("article", `chat-message is-${message.role === "user" ? "user" : "assistant"}${message.mode === "error" ? " is-error" : ""}`);
    item.append(
      make("span", "chat-message-label", message.role === "user" ? "You" : settings.appName),
      make("div", "chat-bubble", message.content)
    );
    if (message.sources?.length) {
      const sources = make("div", "chat-sources");
      message.sources.forEach((source, sourceIndex) => {
        if (source?.product && productKey(source.product)) {
          appendProductCard(sources, source, messageIndex, sourceIndex);
          return;
        }
        const href = safeUrl(source.url || source.href || source.agent_url);
        if (!href) return;
        const link = make("a", "chat-source");
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener";
        link.append(make("strong", "", sourceTitle(source)), make("span", "", "View product →"));
        sources.append(link);
      });
      item.append(sources);
    }
    if (message.purchasePrompt && state.selectedProducts.length) {
      const review = make("button", "button button-primary", "Review purchase");
      review.type = "button";
      review.dataset.reviewPurchase = "true";
      review.disabled = state.purchaseBusy;
      item.append(review);
    }
    log.append(item);
  });
  if (state.chatPending) {
    const item = make("article", "chat-message is-assistant");
    const dots = make("div", "typing");
    dots.setAttribute("aria-label", `${settings.appName} is writing`);
    dots.append(make("span"), make("span"), make("span"));
    item.append(make("span", "chat-message-label", settings.appName), dots);
    log.append(item);
  }
  byId("chat-input").disabled = !state.vendor || state.chatPending;
  byId("chat-submit").disabled = !state.vendor || state.chatPending;
  requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  renderChatContext();
}

// Show exactly which storefront bounds apply to agent browsing.
function renderChatContext() {
  const container = byId("chat-context");
  if (!state.vendor) return showState(container, "No active context", "Select a storefront before asking a question.");
  const stats = make("div", "context-stats");
  const recent = latestSync();
  const resourceCount = state.resources.length || syncCount(recent, "resources");
  const recordCount = state.resources.reduce((sum, resource) => sum + Number(resource.record_count || 0), 0) || syncCount(recent, "records");
  for (const [label, value] of [["Storefront", state.vendor.name || shortId(idOf(state.vendor))], ["Resources", resourceCount], ["Published records", recordCount], ["Agent entrypoint", agentHome()]]) {
    const row = make("div", "context-stat");
    row.append(make("span", "", label), make("strong", "", value));
    stats.append(row);
  }
  container.replaceChildren(stats);
  if (state.selectedProducts.length) {
    const currency = state.selectedProducts[0]?.currency || "USD";
    const chosen = make("div", "selected-items");
    chosen.append(make("span", "selected-product-label", "Selected items"));
    for (const item of state.selectedProducts) appendSummaryCard(chosen, item);
    const total = formatAmount(selectionTotal(state.selectedProducts), currency);
    if (total) chosen.append(make("strong", "purchase-total", `Total: ${total}`));
    chosen.append(make("span", "purchase-note", "Availability shown when selected."));
    const actions = make("div", "purchase-actions");
    const cart = make("button", "button button-secondary", "Add to cart");
    cart.type = "button";
    cart.dataset.addToCart = "true";
    const buy = make("button", "button button-primary", "Buy now");
    buy.type = "button";
    buy.dataset.buyNow = "true";
    buy.disabled = state.purchaseBusy;
    actions.append(cart, buy);
    chosen.append(actions);
    if (state.cartItems.length) {
      chosen.append(make("span", "purchase-note", `${state.cartItems.length} products in cart. No payment has been made.`));
    }
    container.append(chosen);
  }
  if (state.purchase && (state.purchasePhase === "review" || state.purchasePhase === "authorized")) {
    const review = make("div", "purchase-review");
    review.append(make("span", "selected-product-label", "Review your purchase"));
    for (const item of state.purchase.items || []) appendSummaryCard(review, item);
    const total = formatAmount(state.purchase.total, state.purchase.currency);
    if (total) review.append(make("strong", "purchase-total", `Total: ${total}`));
    for (const notice of state.purchase.notices || []) review.append(make("span", "purchase-note", notice));
    if (state.purchasePhase === "review") {
      const actions = make("div", "purchase-actions");
      const confirm = make("button", "button button-primary", "Confirm purchase and continue to payment");
      confirm.type = "button";
      confirm.dataset.confirmPurchase = "true";
      confirm.disabled = state.purchaseBusy;
      const cancel = make("button", "button button-quiet", "Cancel");
      cancel.type = "button";
      cancel.dataset.cancelPurchase = "true";
      cancel.disabled = state.purchaseBusy;
      actions.append(confirm, cancel);
      review.append(actions);
    } else {
      review.append(make("span", "purchase-note", "Your purchase is authorized. Payment has not started yet, and inventory was not changed."));
    }
    container.append(review);
  }
}

// Send one bounded question with only recent conversational history.
async function sendChat(event) {
  event.preventDefault();
  const input = byId("chat-input");
  const message = input.value.trim();
  if (!message || !state.vendor || state.chatPending) return;
  const history = state.chat.filter((item) => !item.local).slice(-settings.chatHistoryLimit).map((item) => ({ role: item.role, content: item.content }));
  state.chat.push({ role: "user", content: message });
  state.chatPending = true;
  input.value = "";
  resizeChatInput();
  renderChat();
  try {
    const result = await fetchJson(apiUrl(`/vendors/${encodeURIComponent(idOf(state.vendor))}/chat`), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, history }) });
    state.chat.push({ role: "assistant", content: result?.answer || "No grounded answer was returned.", mode: result?.mode || "grounded", sources: result?.sources || [], trace: result?.trace || [] });
    if (looksLikeBuyIntent(message) && state.selectedProducts.length) {
      state.chat.push({
        role: "assistant",
        content: "Sure. I'll prepare the purchase summary before payment.",
        mode: "selection",
        local: true,
        purchasePrompt: true,
      });
    }
    byId("chat-mode").textContent = result?.mode === "agent" || result?.mode === "deterministic" ? "In conversation" : "Ready";
  } catch (error) {
    state.chat.push({ role: "assistant", content: `I could not retrieve a grounded answer: ${error.message}`, mode: "error", sources: [] });
  } finally {
    state.chatPending = false;
    renderChat();
    input.focus();
  }
}

// Copy visible text using the browser clipboard API and report failures.
async function copyText(text, label) {
  if (!text) return;
  try { await navigator.clipboard.writeText(text); notify(`${label} copied.`); }
  catch { notify(`Could not copy ${label.toLowerCase()}.`, "error"); }
}

// Update search after a short pause so typing does not flood the API.
function handleCatalogSearch() {
  window.clearTimeout(handleCatalogSearch.timer);
  handleCatalogSearch.timer = window.setTimeout(() => {
    state.catalogQuery = byId("catalog-search").value.trim();
    loadRecords();
  }, 280);
}

// Resize the chat composer within its bounded CSS height.
function resizeChatInput() {
  const input = byId("chat-input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
}

// Handle common keyboard shortcuts while respecting active form inputs.
function handleKeyboard(event) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    navigate("catalog");
    byId("catalog-search").focus();
  }
  if (event.key === "Escape" && state.record) {
    state.record = null;
    renderRecords();
    renderRecordInspector();
  }
}

// Keep the active storefront in sync with the selector.
function handleVendorChange(event) {
  const vendor = state.vendors.find((item) => idOf(item) === event.target.value) || null;
  selectVendor(vendor);
}

// Rebuild mapping fields when the source resource changes.
function handleMappingResource(event) {
  state.mappingResource = event.target.value;
  renderMapping();
}

// Submit chat with Enter while preserving Shift+Enter for a newline.
function handleChatKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    byId("chat-form").requestSubmit();
  }
}

// Send one suggested prompt through the same grounded chat path.
function handleSuggestion(event) {
  const button = event.target.closest("button");
  if (!button || !state.vendor) return;
  byId("chat-input").value = button.textContent;
  byId("chat-form").requestSubmit();
}

// Render every view from the shared selected-store state.
function renderAll() {
  renderOverview();
  renderSource();
  renderResources();
  renderRecords();
  renderRecordInspector();
  renderMapping();
  renderActivity();
  renderChat();
  const url = agentHome();
  byId("agent-document-url").textContent = url || "Select a storefront";
}

// Attach all static listeners once; dynamic lists use delegated handlers.
function bindEvents() {
  document.addEventListener("click", handleNavigation);
  document.addEventListener("keydown", handleKeyboard);
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1), false));
  byId("vendor-select").addEventListener("change", handleVendorChange);
  byId("dismiss-notice").addEventListener("click", () => { byId("global-notice").hidden = true; });
  byId("vendor-form").addEventListener("submit", registerVendor);
  byId("mapping-form").addEventListener("submit", saveMapping);
  byId("mapping-resource").addEventListener("change", handleMappingResource);
  byId("price-units").addEventListener("change", updateMappingReadiness);
  byId("default-currency").addEventListener("input", updateMappingReadiness);
  byId("resource-list").addEventListener("click", handleResourceClick);
  byId("records-body").addEventListener("click", handleRecordClick);
  byId("catalog-search").addEventListener("input", handleCatalogSearch);
  byId("overview-sync").addEventListener("click", syncVendor);
  byId("source-sync").addEventListener("click", syncVendor);
  byId("activity-sync").addEventListener("click", syncVendor);
  byId("copy-overview-url").addEventListener("click", () => copyText(byId("overview-agent-url").textContent, "Agent URL"));
  byId("copy-record").addEventListener("click", () => copyText(state.record ? JSON.stringify(state.record, null, 2) : "", "Record JSON"));
  document.querySelector(".segmented").addEventListener("click", chooseAgentDocument);
  byId("copy-agent-json").addEventListener("click", () => copyText(state.agentJson ? JSON.stringify(state.agentJson, null, 2) : "", "Agent JSON"));
  byId("chat-form").addEventListener("submit", sendChat);
  byId("chat-log").addEventListener("click", handleProductSelect);
  byId("chat-context").addEventListener("click", handleProductSelect);
  byId("chat-input").addEventListener("input", resizeChatInput);
  byId("chat-input").addEventListener("keydown", handleChatKeydown);
  byId("prompt-suggestions").addEventListener("click", handleSuggestion);
}

// Bootstrap static states first, then hydrate health and merchant data in parallel.
async function init() {
  bindEvents();
  renderAll();
  navigate(location.hash.slice(1) || "overview", false);
  await Promise.all([loadHealth(), loadVendors()]);
}

init();
