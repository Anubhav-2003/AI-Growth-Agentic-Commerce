import json
from typing import Any

from any_llm import AnyLLM

from config import CommerceConfig, Settings


class ModelGateway:
    """Provide one optional reusable AnyLLM client for grounded catalog chat."""

    def __init__(self, settings: Settings, config: CommerceConfig):
        """Bind shared settings and create at most one reusable provider client."""
        self.settings = settings
        self.config = config
        self.client = self._create_client()

    def _create_client(self) -> AnyLLM | None:
        """Create a provider only when both provider and model were configured."""
        if not self.settings.model_provider or not self.settings.model_name:
            return None
        options = {"api_base": self.settings.model_api_base} if self.settings.model_api_base else {}
        return AnyLLM.create(self.settings.model_provider, **options)

    async def answer(
        self,
        question: str,
        records: list[dict[str, Any]],
        history: list[dict[str, str]] | None = None,
    ) -> tuple[str, str]:
        """Answer from bounded records, falling back deterministically without a model."""
        if not records:
            return self.config.model.no_results, "deterministic"
        if self.client is None:
            return self._summarize(records), "deterministic"
        response = await self.client.acompletion(
            model=self.settings.model_name,
            messages=self._messages(question, records, history or []),
            timeout=self.config.limits.model_timeout_seconds,
        )
        return self._content(response), "model"

    def _messages(
        self,
        question: str,
        records: list[dict[str, Any]],
        history: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Keep prompts bounded and clearly delimit untrusted merchant content."""
        context = json.dumps(records, ensure_ascii=False, default=str)
        context = context[: self.config.limits.chat_context_characters]
        safe_history = [
            {"role": item["role"], "content": item.get("content", "")}
            for item in history
            if item.get("role") in {"user", "assistant"}
        ]
        return [
            {"role": "system", "content": self.config.model.system_prompt},
            *safe_history[-self.config.limits.chat_history_messages :],
            {"role": "user", "content": f"<catalog-data>{context}</catalog-data>\n\n{question}"},
        ]

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
