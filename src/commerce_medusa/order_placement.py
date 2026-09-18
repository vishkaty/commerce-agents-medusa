"""Placing the order in Medusa once payment has been taken outside it. This is the
host's job in the reference architecture: no agent tool calls it. Idempotent per cart:
a cart Medusa has already completed is reported, not completed twice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .medusa_client import MedusaClient, MedusaError

MANUAL_PROVIDER = "pp_system_default"


@dataclass(frozen=True)
class ShippingAddress:
    first_name: str
    last_name: str
    address_1: str
    city: str
    postal_code: str
    country_code: str
    province: str | None = None

    def as_medusa(self) -> dict[str, Any]:
        body = {
            "first_name": self.first_name,
            "last_name": self.last_name,
            "address_1": self.address_1,
            "city": self.city,
            "postal_code": self.postal_code,
            "country_code": self.country_code.lower(),
        }
        if self.province:
            body["province"] = self.province
        return body


@dataclass(frozen=True)
class PlacedOrder:
    order_id: str
    display_id: int | None
    total: float
    currency: str
    already_placed: bool = False


async def cheapest_shipping_option(
    client: MedusaClient, cart_id: str, token: str | None
) -> dict[str, Any]:
    data = (
        await client.get("/store/shipping-options", params={"cart_id": cart_id}, token=token) or {}
    )
    options = data.get("shipping_options") or []
    if not options:
        raise MedusaError(
            400, "no shipping option serves this cart's address", "/store/shipping-options"
        )

    def fee(option: dict[str, Any]) -> float:
        amount = option.get("amount")
        if amount is None:
            amount = (option.get("calculated_price") or {}).get("calculated_amount") or 0
        return float(amount)

    return min(options, key=fee)


class PaymentMismatch(Exception):
    """The amount the payment provider collected is not the cart's total at completion
    (a price or a line changed between the handoff and the payment). The order is not
    placed; the host holds the payment for review (conformance statement E2E-08)."""

    def __init__(self, cart_id: str, expected_cents: int, paid_cents: int) -> None:
        self.cart_id, self.expected_cents, self.paid_cents = cart_id, expected_cents, paid_cents
        super().__init__(
            f"cart {cart_id} totals {expected_cents} cents at completion but {paid_cents} "
            "cents were paid"
        )


async def place_order(
    client: MedusaClient,
    *,
    cart_id: str,
    email: str,
    address: ShippingAddress,
    token: str | None = None,
    payment_provider: str = MANUAL_PROVIDER,
    payment_reference: str | None = None,
    paid_amount_cents: int | None = None,
) -> PlacedOrder:
    """Address and email on the cart, the cheapest shipping method, a payment session on
    the manual provider (the money already moved on Stripe), then completion. With
    ``paid_amount_cents`` the cart's total after shipping must match it, or nothing is
    completed (:class:`PaymentMismatch`)."""
    data = await client.get(f"/store/carts/{cart_id}", token=token, allow_404=True)
    cart = (data or {}).get("cart")
    if cart is None:
        raise MedusaError(404, f"cart {cart_id} not found", f"/store/carts/{cart_id}")
    if cart.get("completed_at"):
        # Medusa keeps the cart after completion; the order is found by its cart id.
        orders = (
            await client.get(
                "/store/orders",
                params={"limit": 20, "fields": "id,display_id,total,currency_code,cart_id"},
                token=token,
            )
            or {}
        )
        for order in orders.get("orders") or []:
            if order.get("cart_id") == cart_id:
                return PlacedOrder(
                    str(order["id"]),
                    order.get("display_id"),
                    float(order.get("total") or 0),
                    str(order.get("currency_code", "")).upper(),
                    already_placed=True,
                )
        raise MedusaError(409, f"cart {cart_id} is already completed", f"/store/carts/{cart_id}")

    await client.post(
        f"/store/carts/{cart_id}",
        {
            "email": email,
            "shipping_address": address.as_medusa(),
            "billing_address": address.as_medusa(),
        },
        token=token,
    )
    option = await cheapest_shipping_option(client, cart_id, token)
    await client.post(
        f"/store/carts/{cart_id}/shipping-methods", {"option_id": option["id"]}, token=token
    )
    if paid_amount_cents is not None:
        priced = (await client.get(f"/store/carts/{cart_id}", token=token) or {}).get("cart") or {}
        expected_cents = round(float(priced.get("total") or 0) * 100)
        if expected_cents != int(paid_amount_cents):
            raise PaymentMismatch(cart_id, expected_cents, int(paid_amount_cents))
    collection = await client.post("/store/payment-collections", {"cart_id": cart_id}, token=token)
    collection_id = collection["payment_collection"]["id"]
    session_body: dict[str, Any] = {"provider_id": payment_provider}
    if payment_reference:
        session_body["data"] = {"reference": payment_reference}
    await client.post(
        f"/store/payment-collections/{collection_id}/payment-sessions", session_body, token=token
    )
    result = await client.post(f"/store/carts/{cart_id}/complete", {}, token=token)
    if result.get("type") != "order":
        raise MedusaError(
            400,
            f"cart completion refused: {result.get('error') or result}",
            f"/store/carts/{cart_id}/complete",
        )
    order = result["order"]
    return PlacedOrder(
        str(order["id"]),
        order.get("display_id"),
        float(order.get("total") or 0),
        str(order.get("currency_code", "")).upper(),
    )
