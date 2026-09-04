from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse

import httpx
from any_llm import AnyLLM
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, ValidationError

from config import CommerceConfig, Settings
from models import BrowserDecision

LOGGER = logging.getLogger(__name__)


class ProviderDecision(BaseModel):
    """Capture provider JSON before CommerceOS XOR rules run.

    AnyLLM validates ``response_format`` itself. Passing ``BrowserDecision`` there
    rejects recoverable navigation extras (null inputs, citations on follow) before
    application code can run. This envelope is not the public decision contract.
    """

    model_config = ConfigDict(extra="ignore")

    operation: Literal["follow", "submit", "answer"]
    target: str | None = None
    inputs: dict[str, Any] | None = None
    answer: str | None = None
    citations: list[str] | None = None


def _unwrap_scalar(value: Any) -> Any:
    """Read a stored scalar, including the reversible numeric wrappers used in catalogs."""
    if isinstance(value, Mapping) and value.get("$commerceos_type") in {
        "decimal",
        "integer",
        "float",
    }:
        return value.get("value")
    return value


def _first_scalar(data: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    """Return the first non-empty scalar for an alias list without inventing values."""
    for key in keys:
        value = _unwrap_scalar(data.get(key))
        if value is not None and not isinstance(value, (dict, list, bytes, bool)):
            text = str(value).strip()
            if text:
                return text
    return None


def _major_amount(value: Any, *, minor: bool = False) -> int | float | None:
    """Expose a catalog price in major units when the stored amount is finite and labeled."""
    raw = _unwrap_scalar(value)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if minor:
        amount = amount / Decimal("100")
    if not amount.is_finite() or amount < 0:
        return None
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def _catalog_layers(
    item: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any], Mapping[str, Any]]:
    """Split record identity, source fields, and projection from either agent-page shape."""
    nested = item.get("data") if isinstance(item.get("data"), Mapping) else {}
    envelope = any(key in item for key in ("_id", "sync_id", "relationships")) and "data" in item
    if envelope:
        commerce = item.get("commerce") if isinstance(item.get("commerce"), Mapping) else {}
        return str(item.get("_id") or ""), nested, commerce
    if isinstance(nested, Mapping) and any(
        key in nested for key in ("_id", "sync_id", "relationships")
    ):
        source = nested.get("data") if isinstance(nested.get("data"), Mapping) else {}
        commerce = (
            nested.get("commerce")
            if isinstance(nested.get("commerce"), Mapping)
            else item.get("commerce")
            if isinstance(item.get("commerce"), Mapping)
            else {}
        )
        return str(nested.get("_id") or item.get("id") or ""), source, commerce
    commerce = item.get("commerce") if isinstance(item.get("commerce"), Mapping) else {}
    return str(item.get("id") or item.get("_id") or ""), nested, commerce


def _catalog_product(
    item: Mapping[str, Any], aliases: Mapping[str, Sequence[str]]
) -> dict[str, Any] | None:
    """Copy selectable product facts from catalog pages; never from model-generated text."""
    record_id, source, commerce = _catalog_layers(item)
    name = str(commerce.get("title") or "").strip() or _first_scalar(
        source, aliases.get("title", ())
    )
    if not record_id or not name or name == record_id:
        return None
    product: dict[str, Any] = {"record_id": record_id, "name": name}
    mapped = str(commerce.get("id") or "").strip() or _first_scalar(source, aliases.get("id", ()))
    if mapped and mapped != record_id:
        product["id"] = mapped
    sku = _first_scalar(source, ("sku",))
    if sku:
        product["sku"] = sku
    brand = str(commerce.get("brand") or "").strip() or _first_scalar(
        source, aliases.get("brand", ())
    )
    if brand:
        product["brand"] = brand
    if "price" in commerce:
        price = _major_amount(commerce.get("price"), minor=True)
    else:
        price = _major_amount(source.get("price"))
        if price is None:
            price = _major_amount(_first_scalar(source, aliases.get("price", ())))
    if price is not None:
        product["price"] = price
    currency = str(commerce.get("currency") or "").strip() or _first_scalar(
        source, aliases.get("currency", ())
    )
    if currency:
        product["currency"] = currency.upper()
    availability = str(commerce.get("availability") or "").strip() or _first_scalar(
        source, aliases.get("availability", ())
    )
    if availability:
        product["availability"] = availability
    stock = commerce.get("inventory")
    if stock is None:
        stock = source.get("inventory")
        if stock is None:
            stock = _first_scalar(source, aliases.get("inventory", ()))
    count = _integer_count(stock)
    if count is not None:
        product["inventory"] = count
    return product


def _integer_count(value: Any) -> int | None:
    """Read a catalog stock count without treating selection as inventory mutation."""
    raw = _unwrap_scalar(value)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if amount.is_finite() and amount >= 0 and amount == amount.to_integral_value():
        return int(amount)
    return None


def _human_title(item: Mapping[str, Any], aliases: Sequence[str]) -> str | None:
    """Prefer projected or source display names over opaque record identifiers."""
    _, source, commerce = _catalog_layers(item)
    title = str(commerce.get("title") or "").strip()
    if title:
        return title
    named = _first_scalar(source, aliases)
    if named:
        return named
    identity = item.get("page") if isinstance(item.get("page"), Mapping) else {}
    fallback = str(identity.get("title") or "").strip()
    identifier = str(item.get("_id") or item.get("id") or "")
    return fallback if fallback and fallback != identifier else None


class AgentResponseError(RuntimeError):
    """Identify invalid provider output without exposing its raw content."""


class BrowserState(TypedDict):
    """Keep the bounded state that changes while an agent traverses one store."""

    goal: str
    history: list[dict[str, str]]
    current: dict[str, Any]
    current_url: str
    observations: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    decision: BrowserDecision | None
    answer: str
    error: str | None
    steps: int


@dataclass(frozen=True)
class BrowserContext:
    """Supply request-local HTTP state without storing it in checkpointable graph data."""

    client: httpx.AsyncClient
    origin: str
    store_path: str
    on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None


class ModelGateway:
    """Provide one reusable AnyLLM client for structured browsing and grounded answers."""

    def __init__(self, settings: Settings, config: CommerceConfig):
        """Bind shared settings and create at most one reusable provider client."""
        self.settings = settings
        self.config = config
        self.client = self._create_client()

    @property
    def configured(self) -> bool:
        """True when operator selected a provider and model, even if the client failed."""
        return bool(self.settings.model_provider and self.settings.model_name)

    def _create_client(self) -> AnyLLM | None:
        """Create a provider from Settings. A missing key must not take down the process."""
        if not self.configured:
            return None
        options: dict[str, Any] = {}
        if self.settings.model_api_key:
            options["api_key"] = self.settings.model_api_key.get_secret_value()
        if self.settings.model_api_base:
            options["api_base"] = self.settings.model_api_base
        try:
            return AnyLLM.create(self.settings.model_provider, **options)
        except Exception as error:
            LOGGER.warning(
                "AnyLLM provider client could not be created (%s)",
                type(error).__name__,
            )
            return None

    async def decide(
        self,
        goal: str,
        page: Mapping[str, Any],
        observations: list[dict[str, Any]],
        history: list[dict[str, str]],
        *,
        force_answer: bool,
        error: str | None,
    ) -> BrowserDecision:
        """Ask AnyLLM for one schema-validated page transition or final answer."""
        if self.client is None:
            raise AgentResponseError(self.config.model.unavailable)
        context_limit = self.config.limits.chat_context_characters
        visited = self._page_excerpt(observations[:-1], context_limit // 2)
        current = self._page_excerpt(page, context_limit - len(visited))
        instruction = (
            "The navigation limit is reached. Return operation=answer now using the best "
            "visited evidence; do not request another transition."
            if force_answer
            else (
                "If visited pages already contain the facts needed for the user's question, "
                "return operation=answer with shopper text and optional record-href citations. "
                "Otherwise choose one follow or submit with a required target and without "
                "answer text or citations."
            )
        )
        feedback = f"\n<navigation-feedback>{error}</navigation-feedback>" if error else ""
        prompt = (
            f"<user-goal>{goal}</user-goal>\n"
            f"<visited-agent-pages>{visited}</visited-agent-pages>\n"
            f"<current-agent-page>{current}</current-agent-page>{feedback}\n{instruction}"
        )
        try:
            response = await self.client.acompletion(
                model=self.settings.model_name,
                messages=[
                    {"role": "system", "content": self.config.model.system_prompt},
                    *self._safe_history(history),
                    {"role": "user", "content": prompt},
                ],
                response_format=ProviderDecision,
                timeout=self.config.limits.model_timeout_seconds,
            )
        except Exception as error:
            LOGGER.exception("Model decision failed: %s", type(error).__name__)
            raise AgentResponseError(
                "The model provider could not choose a page action."
            ) from error
        return self._decision(response)

    def _safe_history(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        """Retain only bounded user/assistant turns and never accept a stored system role."""
        safe = [
            {"role": item["role"], "content": item.get("content", "")}
            for item in history
            if item.get("role") in {"user", "assistant"}
        ]
        return safe[-self.config.limits.chat_history_messages :]

    def _page_excerpt(self, value: Any, limit: int) -> str:
        """Bound model-visible JSON text while putting executable controls before bulk data."""
        if isinstance(value, Mapping):
            value = {
                key: value.get(key)
                for key in ("page", "links", "actions", "entities", "data", "meta")
                if key in value
            }
        return json.dumps(value, ensure_ascii=False, default=str)[: max(limit, 0)]

    def _summarize(self, records: list[dict[str, Any]]) -> str:
        """Return useful exact matches when no model credentials are available."""
        aliases = self.config.mapping.aliases.get("title", ("title", "name"))
        lines = [self.config.model.deterministic_intro]
        for record in records[: self.config.limits.chat_context_records]:
            data = record.get("data", record)
            name = _human_title(record, aliases) or "Catalog record"
            values = [
                str(value)
                for value in (data.values() if isinstance(data, Mapping) else [])
                if self._is_scalar(value) and str(value).strip() != name
            ]
            detail = " · ".join(values[:4])
            lines.append(f"{name} · {detail}" if detail else name)
        return "\n".join(lines)

    @staticmethod
    def _provider_payload(response: Any) -> Any:
        """Read AnyLLM parsed output or textual JSON without trusting extra provider keys."""
        message = response.choices[0].message
        parsed = getattr(message, "parsed", None)
        if parsed is not None:
            return parsed
        content = ModelGateway._content(response)
        return json.loads(content) if content else {}

    @staticmethod
    def _recover_navigation(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Drop only harmless extras on follow/submit; never invent a target or discard answers."""
        recovered = {
            key: payload[key]
            for key in ("operation", "target", "inputs", "answer", "citations")
            if key in payload
        }
        if recovered.get("citations") is None:
            recovered["citations"] = []
        if recovered.get("operation") not in {"follow", "submit"}:
            if recovered.get("inputs") is None:
                recovered["inputs"] = {}
            return recovered
        answer = recovered.get("answer")
        if isinstance(answer, str) and not answer.strip():
            answer = None
        if answer is not None:
            return recovered
        recovered["answer"] = None
        recovered["citations"] = []
        if recovered.get("inputs") is None:
            recovered["inputs"] = {}
        return recovered

    @staticmethod
    def _decision(response: Any) -> BrowserDecision:
        """Normalize provider JSON, then enforce the public BrowserDecision XOR contract."""
        try:
            parsed = ModelGateway._provider_payload(response)
            if isinstance(parsed, BrowserDecision):
                return parsed
            if isinstance(parsed, ProviderDecision):
                parsed = parsed.model_dump()
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            if not isinstance(parsed, Mapping):
                raise TypeError("The model returned an invalid browser decision.")
            return BrowserDecision.model_validate(ModelGateway._recover_navigation(parsed))
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
            summary: Any = type(error).__name__
            if isinstance(error, ValidationError):
                summary = error.errors(
                    include_url=False, include_input=False, include_context=False
                )
            LOGGER.warning(
                "BrowserDecision validation failed: %s %s", type(error).__name__, summary
            )
            raise AgentResponseError("The model returned an invalid browser decision.") from error

    @staticmethod
    def _is_scalar(value: Any) -> bool:
        """Limit deterministic summaries to readable non-empty scalar values."""
        return (
            value is not None
            and not isinstance(value, (dict, list, bytes))
            and str(value).strip() != ""
        )

    @staticmethod
    def _content(response: Any) -> str:
        """Normalize provider-compatible message content into plain text."""
        content = response.choices[0].message.content
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        parts = [
            part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
            for part in content
        ]
        return "\n".join(part for part in parts if part)


class AgentBrowser:
    """Run a bounded LangGraph loop over controls exposed by the real agent website."""

    def __init__(self, model: ModelGateway, app: Any, config: CommerceConfig) -> None:
        """Compile one reusable graph while keeping each HTTP client request-local."""
        self.model, self.app, self.config = model, app, config
        graph = StateGraph(BrowserState, context_schema=BrowserContext)
        graph.add_node("decide", self._decide)
        graph.add_node("navigate", self._navigate)
        graph.add_edge(START, "decide")
        graph.add_conditional_edges("decide", self._route)
        graph.add_edge("navigate", "decide")
        self.graph = graph.compile(name="commerceos-agent-browser")

    async def run(
        self,
        goal: str,
        entry_url: str,
        history: list[dict[str, str]] | None = None,
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Open the home page and browse only within that storefront until answering."""
        parsed = urlparse(entry_url)
        context = BrowserContext(
            client=httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                base_url=f"{parsed.scheme}://{parsed.netloc}",
            ),
            origin=f"{parsed.scheme}://{parsed.netloc}",
            store_path=parsed.path.rstrip("/"),
            on_event=on_event,
        )
        async with context.client:
            page, url = await self._request(context, "GET", entry_url)
            await self._emit(
                context,
                {
                    "label": "Opened the live storefront",
                    "detail": str(page.get("page", {}).get("title") or "Store home"),
                    "operation": "open",
                },
            )
            observation = {"url": url, "page": page}
            trace = [self._trace(0, "open", url, page)]
            if self.model.client is None:
                if self.model.configured:
                    raise AgentResponseError(self.config.model.unavailable)
                return await self._deterministic(goal, page, observation, trace, context)
            state: BrowserState = {
                "goal": goal,
                "history": history or [],
                "current": page,
                "current_url": url,
                "observations": [observation],
                "trace": trace,
                "decision": None,
                "answer": "",
                "error": None,
                "steps": 0,
            }
            result = await self.graph.ainvoke(
                state,
                config={"recursion_limit": self.config.limits.agent_max_steps * 2 + 4},
                context=context,
            )
        decision = result.get("decision")
        selected = decision.citations if isinstance(decision, BrowserDecision) else []
        return {
            "answer": result["answer"],
            "mode": "agent",
            "sources": self._sources(result["observations"], selected),
            "trace": result["trace"],
        }

    async def _decide(self, state: BrowserState) -> dict[str, Any]:
        """Let the model choose one current affordance, forcing an answer at the step bound."""
        force_answer = state["steps"] >= self.config.limits.agent_max_steps
        decision = await self.model.decide(
            state["goal"],
            state["current"],
            state["observations"],
            state["history"],
            force_answer=force_answer,
            error=state["error"],
        )
        if force_answer and decision.operation != "answer":
            decision = BrowserDecision(operation="answer", answer=self._best_effort(state))
        return {
            "decision": decision,
            "answer": decision.answer or "",
            "error": None,
        }

    async def _navigate(
        self, state: BrowserState, runtime: Runtime[BrowserContext]
    ) -> dict[str, Any]:
        """Validate and execute exactly one control advertised by the current page."""
        decision = state["decision"]
        if decision is None:
            raise AgentResponseError("The browser graph attempted navigation without a decision.")
        step = state["steps"] + 1
        try:
            method, target, inputs = self._transition(state["current"], decision)
            page, url = await self._request(runtime.context, method, target, inputs)
        except (ValueError, SchemaValidationError, httpx.HTTPError) as error:
            feedback = f"The requested transition was rejected: {error}"
            return {
                "steps": step,
                "error": feedback,
                "trace": [
                    *state["trace"],
                    self._trace(step, decision.operation, None, None, feedback),
                ],
            }
        observation = {"url": url, "page": page}
        await self._emit(
            runtime.context,
            {
                "label": self._event_label(decision.operation, page),
                "detail": str(page.get("page", {}).get("title") or "Catalog page"),
                "operation": decision.operation,
            },
        )
        return {
            "current": page,
            "current_url": url,
            "observations": [*state["observations"], observation],
            "trace": [*state["trace"], self._trace(step, decision.operation, url, page)],
            "steps": step,
            "error": None,
        }

    def _route(self, state: BrowserState) -> str:
        """End only on a validated answer; all other decisions visit another page."""
        decision = state["decision"]
        return END if decision and decision.operation == "answer" else "navigate"

    def _transition(
        self, page: Mapping[str, Any], decision: BrowserDecision
    ) -> tuple[str, str, dict[str, Any]]:
        """Resolve a model decision solely against the current page's advertised controls."""
        if decision.operation == "follow":
            candidates = [*page.get("links", []), *page.get("entities", [])]
            targets = {
                str(item["href"])
                for item in candidates
                if isinstance(item, Mapping) and item.get("href")
            }
            if decision.target not in targets:
                raise ValueError("The href is not advertised on the current page.")
            return "GET", str(decision.target), {}
        actions = {
            str(item.get("id")): item
            for item in page.get("actions", [])
            if isinstance(item, Mapping) and item.get("id")
        }
        action = actions.get(str(decision.target))
        if action is None:
            raise ValueError("The action is not advertised on the current page.")
        Draft202012Validator(dict(action.get("input_schema") or {})).validate(decision.inputs)
        return str(action.get("method", "GET")).upper(), str(action["href"]), decision.inputs

    async def _request(
        self,
        context: BrowserContext,
        method: str,
        target: str,
        inputs: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Perform one same-store ASGI request and require a successful JSON representation."""
        parsed = urlparse(target)
        if f"{parsed.scheme}://{parsed.netloc}" != context.origin or not (
            parsed.path == context.store_path or parsed.path.startswith(f"{context.store_path}/")
        ):
            raise ValueError("The transition leaves the current storefront.")
        request_target = (
            str(httpx.URL(target).copy_merge_params(inputs or {})) if method == "GET" else target
        )
        options = {} if method == "GET" else {"json": dict(inputs or {})}
        response = await context.client.request(method, request_target, **options)
        response.raise_for_status()
        try:
            page = response.json()
        except ValueError as error:
            raise ValueError("The transition did not return a JSON page.") from error
        if not isinstance(page, dict):
            raise ValueError("The transition did not return a JSON object.")
        return page, str(response.url)

    async def _deterministic(
        self,
        goal: str,
        home: dict[str, Any],
        observation: dict[str, Any],
        trace: list[dict[str, Any]],
        context: BrowserContext,
    ) -> dict[str, Any]:
        """Demonstrate the same home-to-search website path when no model is configured."""
        decision = BrowserDecision(
            operation="submit",
            target="search",
            inputs={"q": goal, "limit": self.config.limits.chat_context_records},
        )
        try:
            method, target, inputs = self._transition(home, decision)
            page, url = await self._request(context, method, target, inputs)
        except (ValueError, SchemaValidationError, httpx.HTTPError):
            return {
                "answer": self.config.model.no_results,
                "mode": "deterministic",
                "sources": [],
                "trace": trace,
            }
        observations = [observation, {"url": url, "page": page}]
        await self._emit(
            context,
            {
                "label": "Searched the published catalog",
                "detail": str(page.get("page", {}).get("title") or goal),
                "operation": "search",
            },
        )
        entities = [item for item in page.get("entities", []) if isinstance(item, dict)]
        records = [
            {
                "_id": item.get("id"),
                "resource": item.get("resource"),
                "data": item.get("data", {}),
            }
            for item in entities
        ]
        matched = [str(item["href"]) for item in entities if item.get("href")]
        return {
            "answer": self.model._summarize(records) if records else self.config.model.no_results,
            "mode": "deterministic",
            "sources": self._sources(observations, matched),
            "trace": [*trace, self._trace(1, "submit", url, page)],
        }

    @staticmethod
    async def _emit(context: BrowserContext, event: dict[str, Any]) -> None:
        """Report safe browsing activity without exposing prompts or model reasoning."""
        if context.on_event is None:
            return
        try:
            await context.on_event(event)
        except Exception:
            LOGGER.debug("Chat activity listener stopped accepting events", exc_info=True)

    @staticmethod
    def _event_label(operation: str, page: Mapping[str, Any]) -> str:
        """Turn a machine transition into concise shopper-facing activity copy."""
        page_type = str(page.get("page", {}).get("type") or "")
        if page_type == "search-results":
            return "Searched the published catalog"
        if page_type == "record":
            return "Checked a product record"
        if page_type in {"resource", "resource-collection"}:
            return "Browsed the catalog"
        return "Followed a storefront link" if operation == "follow" else "Checked live store data"

    def _best_effort(self, state: BrowserState) -> str:
        """End safely with exact evidence if a provider ignores the forced-answer instruction."""
        records = []
        for observation in state["observations"]:
            page = observation["page"]
            records.extend(
                {
                    "_id": item.get("id"),
                    "resource": item.get("resource"),
                    "data": item.get("data", {}),
                }
                for item in page.get("entities", [])
                if isinstance(item, Mapping) and item.get("type") == "record"
            )
            if page.get("page", {}).get("type") == "record":
                records.append(dict(page.get("data") or {}))
        return self.model._summarize(records) if records else self.config.model.no_results

    def _source_entry(
        self, href: str, label: str, item: Mapping[str, Any], title: str
    ) -> dict[str, Any]:
        """Keep title/label/href and attach catalog product facts when a record is selectable."""
        source: dict[str, Any] = {"label": label, "href": href, "title": title}
        product = _catalog_product(item, self.config.mapping.aliases)
        if product:
            source["product"] = product
        return source

    def _sources(
        self, observations: list[dict[str, Any]], selected: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Prefer cited or opened records; do not promote incidental list/search entities."""
        aliases = self.config.mapping.aliases.get("title", ("title", "name"))
        found: dict[str, dict[str, Any]] = {}
        opened: set[str] = set()
        for observation in observations:
            page, url = observation["page"], str(observation["url"])
            identity = page.get("page", {})
            if identity.get("type") == "record":
                record = page.get("data", {}) if isinstance(page.get("data"), Mapping) else {}
                label = (
                    f"{record.get('resource', 'record')}/{record.get('_id', identity.get('title'))}"
                )
                title = _human_title({**record, "page": identity}, aliases) or identity.get("title")
                found[url] = self._source_entry(url, label, record, str(title or label))
                opened.add(url)
            for item in page.get("entities", []):
                if isinstance(item, Mapping) and item.get("type") == "record" and item.get("href"):
                    href = str(item["href"])
                    label = f"{item.get('resource', 'record')}/{item.get('id', '')}"
                    found[href] = self._source_entry(
                        href, label, item, str(_human_title(item, aliases) or label)
                    )
        requested = [href for href in selected or [] if href in found]
        preferred = requested or [href for href in found if href in opened]
        return [found[href] for href in dict.fromkeys(preferred)][
            : self.config.limits.chat_context_records
        ]

    @staticmethod
    def _trace(
        step: int,
        operation: str,
        url: str | None,
        page: Mapping[str, Any] | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Expose navigation facts without leaking model reasoning or hidden prompts."""
        item: dict[str, Any] = {"step": step, "operation": operation}
        if url:
            item["url"] = url
        if page:
            item["page_type"] = page.get("page", {}).get("type", "document")
            item["title"] = page.get("page", {}).get("title")
        if error:
            item["error"] = error
        return item
