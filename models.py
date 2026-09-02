from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """Reject accidental fields and normalize surrounding string whitespace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceConfig(StrictModel):
    """Describe one merchant source without assuming its internal schema."""

    kind: str = Field(min_length=1)
    path: str = Field(min_length=1)


class CommerceMapping(StrictModel):
    """Store an explicit, auditable projection from source fields to commerce meaning."""

    resource: str = Field(min_length=1)
    fields: dict[str, str] = Field(default_factory=dict)
    price_units: Literal["major", "minor"] | None = None
    default_currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("default_currency")
    @classmethod
    def uppercase_currency(cls, value: str | None) -> str | None:
        """Normalize explicit ISO currency codes without guessing missing values."""
        return value.upper() if value else None


class VendorCreate(StrictModel):
    """Validate merchant onboarding before a source is ever opened."""

    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    source: SourceConfig
    mapping: CommerceMapping | None = None
    public: bool = True


class VendorPatch(StrictModel):
    """Allow small vendor edits while preventing arbitrary Mongo updates."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    source: SourceConfig | None = None
    mapping: CommerceMapping | None = None
    public: bool | None = None


class MappingUpdate(StrictModel):
    """Replace a merchant-approved mapping as one versioned unit."""

    mapping: CommerceMapping


class ChatRequest(StrictModel):
    """Bound a grounded catalog question and optional conversation history."""

    message: str = Field(min_length=1)
    history: list[dict[str, str]] = Field(default_factory=list)


class PurchaseLineInput(StrictModel):
    """Accept a shopper selection snapshot; catalog identity and price stay server-side."""

    record_id: str = Field(min_length=1, max_length=128)
    quantity: int = Field(ge=1, le=10000)
    displayed_price: float | None = Field(default=None, ge=0)


class PurchaseReviewRequest(StrictModel):
    """Open a purchase review from selected lines without treating it as payment."""

    items: list[PurchaseLineInput] = Field(min_length=1)


class PurchaseAuthorizeRequest(StrictModel):
    """Require an unambiguous confirmation and an optional spending ceiling."""

    confirm: bool
    max_amount: float | None = Field(default=None, ge=0)


class PaymentVerifyRequest(StrictModel):
    """Accept Checkout callback identifiers; retries may reuse stored values."""

    razorpay_payment_id: str | None = Field(default=None, max_length=64)
    razorpay_order_id: str | None = Field(default=None, max_length=64)
    razorpay_signature: str | None = Field(default=None, max_length=256)

    @field_validator("razorpay_payment_id", "razorpay_order_id", "razorpay_signature")
    @classmethod
    def blank_optional_callback(cls, value: str | None) -> str | None:
        """Treat empty callback fields as omitted so a retry can use stored identifiers."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class BrowserDecision(StrictModel):
    """Represent one exact machine-page transition or the final grounded answer."""

    operation: Literal["follow", "submit", "answer"]
    target: str | None = Field(default=None, description="Exact href or current action ID")
    inputs: dict[str, Any] = Field(default_factory=dict)
    answer: str | None = None
    citations: list[str] = Field(
        default_factory=list, description="Exact encountered record hrefs supporting an answer"
    )

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "BrowserDecision":
        """Require only the fields meaningful for the selected browser operation."""
        if self.operation == "answer":
            if not self.answer or self.target is not None or self.inputs:
                raise ValueError("Answer decisions require only non-empty answer text.")
        elif not self.target or self.answer is not None or self.citations:
            raise ValueError("Navigation decisions require a target and no answer text.")
        elif self.operation == "follow" and self.inputs:
            raise ValueError("Follow decisions cannot include action inputs.")
        return self


class Link(BaseModel):
    """Represent RFC/IANA navigation relations in an agent page."""

    rel: list[str]
    href: str
    title: str | None = None
    type: str | None = None


class Action(BaseModel):
    """Describe a currently valid transition with a generated input schema."""

    id: str
    title: str
    method: str
    href: str
    content_type: str
    input_schema: dict[str, Any]


class PageIdentity(BaseModel):
    """Give every machine page a stable identity and semantic type."""

    id: str
    type: str
    title: str
    summary: str | None = None


class AgentPage(BaseModel):
    """Combine state, links, entities, and actions into the agent website format."""

    page: PageIdentity
    data: Any
    entities: list[Any] = Field(default_factory=list)
    links: list[Link]
    actions: list[Action] = Field(default_factory=list)
    meta: dict[str, Any]


class Problem(BaseModel):
    """Return RFC 9457-compatible protocol errors without leaking internals."""

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
