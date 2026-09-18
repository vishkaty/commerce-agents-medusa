"""Stripe hosted checkout for the lab, test mode only: a Checkout Session per cart, the
webhook signature check, and nothing else. Raw REST over httpx (form-encoded) so the
transport can be mocked in tests and there is no SDK to pin.

The agent never sees these URLs: ``MedusaStorefront.checkout_handoff`` returns the
session URL to the executor, which puts it on the checkout card for the host to render.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

import httpx
from shopping_agent import Cart

STRIPE_API = "https://api.stripe.com"
SIGNATURE_TOLERANCE_S = 300


class StripeError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"stripe {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class CheckoutLine:
    name: str
    unit_amount_cents: int
    quantity: int


def _flatten(prefix: str, value: Any, out: dict[str, str]) -> None:
    """Stripe's bracket form encoding: ``line_items[0][price_data][currency]``."""
    if isinstance(value, dict):
        for key, inner in value.items():
            _flatten(f"{prefix}[{key}]", inner, out)
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            _flatten(f"{prefix}[{index}]", inner, out)
    elif isinstance(value, bool):
        out[prefix] = "true" if value else "false"
    elif value is not None:
        out[prefix] = str(value)


def form_encode(body: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in body.items():
        _flatten(key, value, out)
    return out


def cents(amount: float) -> int:
    return round(float(amount) * 100)


def checkout_lines(
    cart: Cart, shipping_name: str | None, shipping_fee: float
) -> list[CheckoutLine]:
    lines = [
        CheckoutLine(name=item.title, unit_amount_cents=cents(item.price), quantity=item.quantity)
        for item in cart.items
    ]
    if shipping_name and shipping_fee > 0:
        lines.append(
            CheckoutLine(name=shipping_name, unit_amount_cents=cents(shipping_fee), quantity=1)
        )
    return lines


class StripeClient:
    def __init__(
        self,
        secret_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = STRIPE_API,
        timeout: float = 20.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, auth=(secret_key, ""), transport=transport, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._http.post(path, data=form_encode(body))
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            raise StripeError(
                response.status_code, str((data.get("error") or {}).get("message", data))[:300]
            )
        return data

    async def _get(self, path: str) -> dict[str, Any]:
        response = await self._http.get(path)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            raise StripeError(
                response.status_code, str((data.get("error") or {}).get("message", data))[:300]
            )
        return data

    async def create_checkout_session(
        self,
        *,
        lines: list[CheckoutLine],
        currency: str,
        success_url: str,
        cancel_url: str,
        metadata: dict[str, str],
        customer_email: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": metadata.get("medusa_cart_id"),
            "metadata": metadata,
            "line_items": [
                {
                    "quantity": line.quantity,
                    "price_data": {
                        "currency": currency.lower(),
                        "unit_amount": line.unit_amount_cents,
                        "product_data": {"name": line.name[:120]},
                    },
                }
                for line in lines
            ],
        }
        if customer_email:
            body["customer_email"] = customer_email
        return await self._post("/v1/checkout/sessions", body)

    async def retrieve_checkout_session(self, session_id: str) -> dict[str, Any]:
        return await self._get(f"/v1/checkout/sessions/{session_id}")


def verify_webhook_signature(
    payload: bytes, header: str, secret: str, *, now: float | None = None
) -> bool:
    """Stripe's scheme: ``t=<unix>,v1=<hex hmac sha256 of "<t>.<payload>">``; the
    timestamp must be within the tolerance. Constant-time comparison."""
    parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    timestamp = parts.get("t")
    signatures = [
        value
        for key, value in (p.split("=", 1) for p in header.split(",") if "=" in p)
        if key == "v1"
    ]
    if not timestamp or not signatures:
        return False
    try:
        stamp = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - stamp) > SIGNATURE_TOLERANCE_S:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def sign_webhook(payload: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """The header Stripe would send; for tests and the local trigger path."""
    stamp = timestamp if timestamp is not None else int(time.time())
    digest = hmac.new(secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"
