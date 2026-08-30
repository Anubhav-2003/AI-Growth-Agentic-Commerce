from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypedDict
from urllib.parse import urlparse

import httpx
from any_llm import AnyLLM
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from config import CommerceConfig, Settings
from models import BrowserDecision


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


class ModelGateway:
    """Provide one reusable AnyLLM client for structured browsing and grounded answers."""

    def __init__(self, settings: Settings, config: CommerceConfig):
        """Bind shared settings and create at most one reusable provider client."""
        self.settings = settings
        self.config = config
        self.client = self._create_client()

    def _create_client(self) -> AnyLLM | None:
        """Create a provider only when both provider and model were configured."""
        if not self.settings.model_provider or not self.settings.model_name:
            return None
        options = (
            {"api_key": self.settings.model_api_key.get_secret_value()}
            if self.settings.model_api_key
            else {}
        )
        if self.settings.model_api_base:
            options["api_base"] = self.settings.model_api_base
        return AnyLLM.create(self.settings.model_provider, **options)

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
            raise AgentResponseError("A model is required for autonomous browsing.")
        context_limit = self.config.limits.chat_context_characters
        visited = self._page_excerpt(observations[:-1], context_limit // 2)
        current = self._page_excerpt(page, context_limit - len(visited))
        instruction = (
            "The navigation limit is reached. Return operation=answer now using the best "
            "visited evidence; do not request another transition."
            if force_answer
            else "Choose one current-page transition, or answer if the evidence is sufficient."
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
                response_format=BrowserDecision,
                timeout=self.config.limits.model_timeout_seconds,
            )
        except Exception as error:
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
        lines = [self.config.model.deterministic_intro]
        for record in records[: self.config.limits.chat_context_records]:
            data = record.get("data", record)
            values = [str(value) for value in data.values() if self._is_scalar(value)]
            identifier = record.get("_id", record.get("id"))
            label = record.get("label") or f"{record.get('resource', 'record')}/{identifier}"
            lines.append(f"[{label}] {' · '.join(values[:5])}")
        return "\n".join(lines)

    @staticmethod
    def _decision(response: Any) -> BrowserDecision:
        """Prefer provider-parsed output and validate textual JSON as a portable fallback."""
        message = response.choices[0].message
        parsed = getattr(message, "parsed", None)
        if isinstance(parsed, BrowserDecision):
            return parsed
        try:
            if parsed is not None:
                return BrowserDecision.model_validate(parsed)
            return BrowserDecision.model_validate_json(ModelGateway._content(response))
        except (TypeError, ValueError, ValidationError) as error:
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
        self, goal: str, entry_url: str, history: list[dict[str, str]] | None = None
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
        )
        async with context.client:
            page, url = await self._request(context, "GET", entry_url)
            observation = {"url": url, "page": page}
            trace = [self._trace(0, "open", url, page)]
            if self.model.client is None:
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
        entities = [item for item in page.get("entities", []) if isinstance(item, dict)]
        records = [
            {
                "_id": item.get("id"),
                "resource": item.get("resource"),
                "data": item.get("data", {}),
            }
            for item in entities
        ]
        return {
            "answer": self.model._summarize(records) if records else self.config.model.no_results,
            "mode": "deterministic",
            "sources": self._sources(observations),
            "trace": [*trace, self._trace(1, "submit", url, page)],
        }

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

    def _sources(
        self, observations: list[dict[str, Any]], selected: list[str] | None = None
    ) -> list[dict[str, str]]:
        """Collect unique exact record pages encountered during navigation."""
        found: dict[str, dict[str, str]] = {}
        opened: set[str] = set()
        for observation in observations:
            page, url = observation["page"], str(observation["url"])
            identity = page.get("page", {})
            if identity.get("type") == "record":
                record = page.get("data", {})
                label = (
                    f"{record.get('resource', 'record')}/{record.get('_id', identity.get('title'))}"
                )
                found[url] = {"label": label, "href": url}
                opened.add(url)
            for item in page.get("entities", []):
                if isinstance(item, Mapping) and item.get("type") == "record" and item.get("href"):
                    href = str(item["href"])
                    found[href] = {
                        "label": f"{item.get('resource', 'record')}/{item.get('id', '')}",
                        "href": href,
                    }
        requested = [href for href in selected or [] if href in found]
        preferred = requested or [href for href in found if href in opened] or list(found)
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
