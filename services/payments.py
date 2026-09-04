"""Razorpay Test Mode checkout, plus an explicit unavailable agentic-payment adapter."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from config import Settings

LOGGER = logging.getLogger(__name__)


class PaymentUnavailable(ValueError):
    """Describe a missing or inactive payment capability without leaking credentials."""


@dataclass(frozen=True)
class CreatedOrder:
    """Return only the provider order facts needed to start checkout."""

    provider_order_id: str
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class ProviderPayment:
    """Expose a verified provider payment status without card or secret fields."""

    payment_id: str
    order_id: str
    amount_minor: int
    currency: str
    status: str
    captured: bool


class PaymentProvider(Protocol):
    """Keep Standard Checkout and future agentic execution behind one small boundary."""

    name: str

    def available(self) -> bool:
        """True when this provider can create a real payment."""

    def create_payment(
        self, *, amount_minor: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> CreatedOrder:
        """Create a provider order from authoritative backend amounts only."""

    def verify_signature(self, *, order_id: str, payment_id: str, signature: str) -> None:
        """Validate HMAC using the stored order id, never a browser-chosen order id."""

    def get_payment_status(self, payment_id: str) -> ProviderPayment:
        """Fetch the live provider payment after signature verification."""

    def verify_webhook(self, body: bytes, signature: str) -> None:
        """Validate a raw webhook body against the configured webhook secret."""


class DisabledPaymentProvider:
    """Keep purchases reviewable when Razorpay Test Mode is not configured."""

    name = "disabled"

    def available(self) -> bool:
        return False

    def create_payment(self, **_kwargs: Any) -> CreatedOrder:
        raise PaymentUnavailable("Payment is not configured for this storefront.")

    def verify_signature(self, **_kwargs: Any) -> None:
        raise PaymentUnavailable("Payment is not configured for this storefront.")

    def get_payment_status(self, payment_id: str) -> ProviderPayment:
        raise PaymentUnavailable("Payment is not configured for this storefront.")

    def verify_webhook(self, body: bytes, signature: str) -> None:
        raise PaymentUnavailable("Webhook verification is not configured.")


class RazorpayCheckoutProvider:
    """Razorpay Standard Checkout in TEST mode using the official Python client."""

    name = "razorpay_checkout"

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str | None = None) -> None:
        """Bind TEST credentials in memory; never persist or log them."""
        import razorpay

        self.key_id = key_id
        self._secret = key_secret
        self._webhook_secret = webhook_secret
        self._client = razorpay.Client(auth=(key_id, key_secret))

    def available(self) -> bool:
        return True

    def create_payment(
        self, *, amount_minor: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> CreatedOrder:
        """Create an Orders API order in catalog currency subunits without converting FX."""
        try:
            created = self._client.order.create(
                {
                    "amount": int(amount_minor),
                    "currency": str(currency).upper(),
                    "receipt": receipt[:40],
                    "notes": notes,
                }
            )
        except Exception as error:
            LOGGER.warning("Razorpay order creation failed (%s)", type(error).__name__)
            raise ValueError(
                "This purchase could not be sent to the payment provider. "
                "The catalog currency was not converted."
            ) from None
        order_id = str(created.get("id") or "")
        if not order_id:
            raise ValueError("The payment provider did not return an order.")
        return CreatedOrder(
            provider_order_id=order_id,
            amount_minor=int(created.get("amount", amount_minor)),
            currency=str(created.get("currency") or currency).upper(),
        )

    def verify_signature(self, *, order_id: str, payment_id: str, signature: str) -> None:
        """HMAC-SHA256(stored_order_id|payment_id) as required by Razorpay Checkout."""
        expected = hmac.new(
            self._secret.encode("utf-8"),
            f"{order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, str(signature or "")):
            raise ValueError("The payment signature could not be verified.")

    def get_payment_status(self, payment_id: str) -> ProviderPayment:
        """Read captured/authorized state from the Payments API after signature checks."""
        try:
            found = self._client.payment.fetch(payment_id)
        except Exception:
            LOGGER.warning("Razorpay payment fetch failed (%s)", "Payment.fetch")
            raise PaymentUnavailable(
                "Payment may have succeeded, but we couldn't confirm it with Razorpay yet. "
                "Don't pay again. Retry payment confirmation."
            ) from None
        status = str(found.get("status") or "")
        return ProviderPayment(
            payment_id=str(found.get("id") or payment_id),
            order_id=str(found.get("order_id") or ""),
            amount_minor=int(found.get("amount") or 0),
            currency=str(found.get("currency") or "").upper(),
            status=status,
            captured=bool(found.get("captured")) or status == "captured",
        )

    def verify_webhook(self, body: bytes, signature: str) -> None:
        """Validate X-Razorpay-Signature against the raw body and webhook secret."""
        if not self._webhook_secret:
            raise PaymentUnavailable("Webhook verification is not configured.")
        self._client.utility.verify_webhook_signature(
            body.decode("utf-8"), signature, self._webhook_secret
        )


class RazorpayAgenticProvider:
    """Placeholder for UPI Reserve Pay / Agentic Payments; never invents those APIs."""

    name = "razorpay_agentic"

    def available(self) -> bool:
        return False

    def create_payment(self, **_kwargs: Any) -> CreatedOrder:
        raise PaymentUnavailable(agentic_unavailable_message())

    def verify_signature(self, **_kwargs: Any) -> None:
        raise PaymentUnavailable(agentic_unavailable_message())

    def get_payment_status(self, payment_id: str) -> ProviderPayment:
        raise PaymentUnavailable(agentic_unavailable_message())

    def verify_webhook(self, body: bytes, signature: str) -> None:
        raise PaymentUnavailable(agentic_unavailable_message())


def agentic_unavailable_message() -> str:
    """Explain the real access gap without claiming Standard Checkout is agentic."""
    return (
        "Razorpay Agentic Payments and UPI Reserve Pay are not available on this account. "
        "They require Razorpay Support / early-access activation for Single Block Multi Debit. "
        "Standard Checkout remains the working Test Mode path."
    )


def build_payment_provider(settings: Settings) -> PaymentProvider:
    """Construct Standard Checkout when TEST credentials exist; otherwise stay disabled."""
    if not settings.razorpay_enabled:
        return DisabledPaymentProvider()
    key_id = (settings.razorpay_key_id or "").strip()
    secret = settings.razorpay_key_secret.get_secret_value() if settings.razorpay_key_secret else ""
    if not key_id or not secret:
        return DisabledPaymentProvider()
    webhook = (
        settings.razorpay_webhook_secret.get_secret_value()
        if settings.razorpay_webhook_secret
        else None
    )
    return RazorpayCheckoutProvider(key_id, secret, webhook)
