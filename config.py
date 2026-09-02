from functools import cached_property, lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppMeta(BaseModel):
    """Validate shared product metadata used by both web surfaces."""

    name: str
    version: str
    tagline: str
    language: str
    browser_storage_key: str


class RouteConfig(BaseModel):
    """Keep mounted route prefixes in one validated configuration source."""

    api: str
    agent: str
    static: str


class CollectionConfig(BaseModel):
    """Name Mongo collections once so services cannot drift."""

    vendors: str
    resources: str
    records: str
    syncs: str
    purchases: str


class LimitConfig(BaseModel):
    """Bound ingestion, pagination, search, and model context centrally."""

    batch_size: int = Field(gt=0)
    default_page_size: int = Field(gt=0)
    max_page_size: int = Field(gt=0)
    max_source_bytes: int = Field(gt=0)
    max_record_bytes: int = Field(gt=0)
    max_request_bytes: int = Field(gt=0)
    max_query_length: int = Field(gt=0)
    chat_context_records: int = Field(gt=0)
    chat_context_characters: int = Field(gt=0)
    chat_question_characters: int = Field(gt=0)
    chat_history_messages: int = Field(gt=0)
    agent_max_steps: int = Field(gt=0)
    model_timeout_seconds: int = Field(gt=0)
    mongo_timeout_milliseconds: int = Field(gt=0)


class SecurityConfig(BaseModel):
    """Centralize the operator boundary without pretending a vendor ID is auth."""

    admin_header: str
    local_environments: set[str]


class MappingConfig(BaseModel):
    """Validate deterministic commerce aliases and their confidence gate."""

    minimum_score: int = Field(ge=0, le=100)
    aliases: dict[str, list[str]]


class UcpConfig(BaseModel):
    """Centralize the exact UCP release and advertised catalog contracts."""

    version: str
    service: str
    service_spec: str
    service_schema: str
    search_capability: str
    search_spec: str
    search_schema: str
    lookup_capability: str
    lookup_spec: str
    lookup_schema: str


class AgentPageConfig(BaseModel):
    """Version the agent-page representation independently from application code."""

    version: str | float
    content_type: str
    profile_path: str


class ModelConfig(BaseModel):
    """Centralize grounding instructions and deterministic fallback copy."""

    system_prompt: str
    no_results: str
    deterministic_intro: str
    unavailable: str


class CommerceConfig(BaseModel):
    """Represent all non-secret shared CommerceOS configuration."""

    app: AppMeta
    routes: RouteConfig
    collections: CollectionConfig
    limits: LimitConfig
    security: SecurityConfig
    formats: dict[str, list[str]]
    mapping: MappingConfig
    ucp: UcpConfig
    agent_page: AgentPageConfig
    model: ModelConfig


class Settings(BaseSettings):
    """Load deployment values from process environment with `.env` as local fallback."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    config_path: Path
    mongodb_uri: str
    mongodb_database: str
    source_roots: list[Path]
    app_env: str
    app_host: str
    app_port: int = Field(ge=1, le=65535)
    admin_api_key: SecretStr | None = None
    model_provider: str | None = None
    model_name: str | None = None
    model_api_key: SecretStr | None = None
    model_api_base: str | None = None

    @field_validator(
        "admin_api_key",
        "model_provider",
        "model_name",
        "model_api_key",
        "model_api_base",
        mode="before",
    )
    @classmethod
    def blank_optional_values(cls, value: object) -> object | None:
        """Treat blank optional environment values as deliberately unconfigured."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("config_path", mode="after")
    @classmethod
    def resolve_config_path(cls, value: Path) -> Path:
        """Resolve the config once so startup errors reference an absolute path."""
        return value.expanduser().resolve()

    @field_validator("source_roots", mode="after")
    @classmethod
    def resolve_source_roots(cls, values: list[Path]) -> list[Path]:
        """Resolve allow-listed roots before any source security comparison."""
        return [value.expanduser().resolve() for value in values]

    @cached_property
    def commerce(self) -> CommerceConfig:
        """Parse and validate centralized YAML only on first use."""
        descriptor = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        return CommerceConfig.model_validate(descriptor)


@lru_cache
def get_settings() -> Settings:
    """Reuse one settings instance after loading `.env` into the process for libraries."""
    load_dotenv()
    return Settings()  # type: ignore[call-arg]
