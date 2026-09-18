"""Placing a Medusa order after an external payment: the call sequence against a mocked
Store API, idempotency for a completed cart, and the checkout handoff on the storefront."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from shopping_agent import ShoppingSessionContext

from commerce_medusa.medusa_client import MedusaClient, MedusaError
from commerce_medusa.medusa_storefront import CustomerDirectory, MedusaStorefront
from commerce_medusa.order_placement import ShippingAddress, place_order
from commerce_medusa.stripe_checkout import StripeClient

FIXTURES = Path(__file__).parent / "fixtures"
CART = json.loads((FIXTURES / "medusa_cart.json").read_text())["cart"]
SHIPPING = json.loads((FIXTURES / "medusa_shipping_options.json").read_text())
ADDRESS = ShippingAddress("Priya", "Lab", "1 Main St", "Austin", "78701", "US", "tx")


class FakeStore:
    def __init__(self, completed: bool = False) -> None:
        self.completed = completed
        self.calls: list[tuple[str, str, dict | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        path = request.url.path
        if path == f"/store/carts/{CART['id']}" and request.method == "GET":
            cart = dict(CART, completed_at="2026-09-04T10:00:00Z" if self.completed else None)
            return httpx.Response(200, json={"cart": cart})
        if path == f"/store/carts/{CART['id']}" and request.method == "POST":
            return httpx.Response(200, json={"cart": {**CART, **body}})
        if path == "/store/shipping-options":
            return httpx.Response(200, json=SHIPPING)
        if path.endswith("/shipping-methods"):
            return httpx.Response(200, json={"cart": CART})
        if path == "/store/payment-collections":
            return httpx.Response(200, json={"payment_collection": {"id": "pay_col_1"}})
        if path == "/store/payment-collections/pay_col_1/payment-sessions":
            return httpx.Response(
                200,
                json={
                    "payment_collection": {
                        "id": "pay_col_1",
                        "payment_sessions": [
                            {"provider_id": body["provider_id"], "status": "pending"}
                        ],
                    }
                },
            )
        if path == f"/store/carts/{CART['id']}/complete":
            self.completed = True
            return httpx.Response(
                200,
                json={
                    "type": "order",
                    "order": {
                        "id": "order_new",
                        "display_id": 7,
                        "total": 28.0,
                        "currency_code": "usd",
                    },
                },
            )
        if path == "/store/orders":
            return httpx.Response(
                200,
                json={
                    "orders": [
                        {
                            "id": "order_old",
                            "display_id": 3,
                            "total": 20,
                            "currency_code": "eur",
                            "cart_id": CART["id"],
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"message": f"unhandled {request.method} {path}"})


async def test_place_order_runs_the_medusa_sequence():
    store = FakeStore()
    client = MedusaClient("http://medusa.test", "pk", transport=httpx.MockTransport(store.handler))
    placed = await place_order(
        client,
        cart_id=CART["id"],
        email="priya@lab.local",
        address=ADDRESS,
        token="tok",
        payment_reference="cs_test_1",
    )
    assert placed.order_id == "order_new" and placed.display_id == 7 and not placed.already_placed
    paths = [c[1] for c in store.calls]
    assert paths.index(f"/store/carts/{CART['id']}") < paths.index("/store/shipping-options")
    assert paths.index("/store/shipping-options") < paths.index(
        f"/store/carts/{CART['id']}/shipping-methods"
    )
    assert paths.index("/store/payment-collections") < paths.index(
        "/store/payment-collections/pay_col_1/payment-sessions"
    )
    assert paths[-1] == f"/store/carts/{CART['id']}/complete"
    update = next(c for c in store.calls if c[0] == "POST" and c[1] == f"/store/carts/{CART['id']}")
    assert update[2]["email"] == "priya@lab.local"
    assert update[2]["shipping_address"]["country_code"] == "us"
    shipping = next(c for c in store.calls if c[1].endswith("/shipping-methods"))
    cheapest = min(SHIPPING["shipping_options"], key=lambda o: o["amount"])
    assert shipping[2] == {"option_id": cheapest["id"]}
    payment = next(c for c in store.calls if c[1].endswith("/payment-sessions"))
    assert payment[2]["provider_id"] == "pp_system_default"
    assert payment[2]["data"] == {"reference": "cs_test_1"}
    assert all(c[0] != "POST" or "authorization" for c in store.calls)


async def test_completed_cart_reports_the_existing_order():
    store = FakeStore(completed=True)
    client = MedusaClient("http://medusa.test", "pk", transport=httpx.MockTransport(store.handler))
    placed = await place_order(
        client, cart_id=CART["id"], email="priya@lab.local", address=ADDRESS, token="tok"
    )
    assert placed.already_placed and placed.order_id == "order_old"
    assert not any(c[1].endswith("/complete") for c in store.calls)


async def test_unknown_cart_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "no"})

    client = MedusaClient("http://medusa.test", "pk", transport=httpx.MockTransport(handler))
    with pytest.raises(MedusaError):
        await place_order(client, cart_id="cart_missing", email="x@y", address=ADDRESS)


async def test_checkout_handoff_creates_a_stripe_session_for_the_cart():
    store = FakeStore()
    stripe_forms: list[dict] = []

    def stripe_handler(request: httpx.Request) -> httpx.Response:
        stripe_forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200, json={"id": "cs_test_9", "url": "https://checkout.stripe.com/c/pay/cs_test_9"}
        )

    products = json.loads((FIXTURES / "medusa_products.json").read_text())

    def medusa_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/store/carts" and request.method == "POST":
            return httpx.Response(200, json={"cart": {**CART, "items": []}})
        if request.url.path == "/store/products":
            return httpx.Response(200, json=products)
        return store.handler(request)

    client = MedusaClient("http://medusa.test", "pk", transport=httpx.MockTransport(medusa_handler))
    stripe = StripeClient("sk_test", transport=httpx.MockTransport(stripe_handler))
    customers = CustomerDirectory(
        {
            "priya": {
                "token": "tok",
                "email": "priya@lab.local",
                "display_name": "Priya",
                "address": ADDRESS,
            }
        }
    )
    backend = MedusaStorefront(
        client,
        region_id="reg_1",
        customers=customers,
        stripe=stripe,
        host_url="http://localhost:8010/",
    )
    session = ShoppingSessionContext(session_id="s-9", user_id="priya")
    cart = await backend.get_cart(session)  # creates the cart with the address
    from commerce_medusa.medusa_mapping import cart_from_medusa

    cart = cart_from_medusa(CART)  # the fixture cart's line stands in for an add
    handoffs = await backend.checkout_handoff(session, cart)
    assert len(handoffs) == 1
    assert handoffs[0].url == "https://checkout.stripe.com/c/pay/cs_test_9"
    assert handoffs[0].label
    form = stripe_forms[0]
    assert form["metadata[medusa_cart_id]"] == CART["id"]
    assert form["metadata[lab_user_id]"] == "priya"
    assert form["customer_email"] == "priya@lab.local"
    assert form["success_url"].startswith("http://localhost:8010/checkout/success")
    assert form["line_items[0][price_data][unit_amount]"] == "1000"  # EUR 10.00 line
    names = [v for k, v in form.items() if k.endswith("[product_data][name]")]
    assert any("Shipping" in n for n in names), "the cheapest shipping option is a line"


async def test_checkout_handoff_without_stripe_is_empty():
    """No Stripe client: the host's own checkout applies, after the same re-validation."""
    store = FakeStore()
    products = json.loads((FIXTURES / "medusa_products.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/store/products":
            return httpx.Response(200, json=products)
        return store.handler(request)

    client = MedusaClient("http://medusa.test", "pk", transport=httpx.MockTransport(handler))
    backend = MedusaStorefront(client, region_id="reg_1")
    from commerce_medusa.medusa_mapping import cart_from_medusa

    assert (
        await backend.checkout_handoff(
            ShoppingSessionContext(session_id="s", user_id="g"), cart_from_medusa(CART)
        )
        == []
    )


# -- cart re-validation at checkout ----------------------------------------------


async def test_place_order_refuses_a_paid_amount_that_does_not_match_the_cart():
    from commerce_medusa.order_placement import PaymentMismatch

    store = FakeStore()
    client = MedusaClient("http://medusa.test", "pk", transport=httpx.MockTransport(store.handler))
    with pytest.raises(PaymentMismatch) as raised:
        await place_order(
            client,
            cart_id=CART["id"],
            email="priya@lab.local",
            address=ADDRESS,
            token="tok",
            paid_amount_cents=1500,  # the fixture cart totals 20.00
        )
    assert raised.value.expected_cents == 2000 and raised.value.paid_cents == 1500
    assert not any(c[1].endswith("/complete") for c in store.calls), "nothing was completed"
    placed = await place_order(
        client, cart_id=CART["id"], email="priya@lab.local", address=ADDRESS, paid_amount_cents=2000
    )
    assert placed.order_id == "order_new"


def _storefront_with_stripe(fake, stripe_forms: list[dict]) -> MedusaStorefront:
    from tests.test_medusa_storefront import REGION

    def stripe_handler(request: httpx.Request) -> httpx.Response:
        stripe_forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200, json={"id": "cs_test_9", "url": "https://checkout.stripe.com/c/pay/cs_test_9"}
        )

    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    stripe = StripeClient("sk_test", transport=httpx.MockTransport(stripe_handler))
    customers = CustomerDirectory({"priya": {"token": "tok-priya", "email": "priya@lab.local"}})
    return MedusaStorefront(
        client, region_id=REGION, customers=customers, stripe=stripe, host_url="http://h"
    )


async def test_checkout_handoff_refuses_a_line_that_is_no_longer_purchasable():
    from shopping_agent.backend import Unavailable

    from tests.test_medusa_storefront import XL, FakeMedusa

    fake = FakeMedusa()
    forms: list[dict] = []
    backend = _storefront_with_stripe(fake, forms)
    session = ShoppingSessionContext(session_id="s-stale", user_id="priya")
    await backend.add_to_cart(session, XL["id"], 2)
    fake.out_of_stock.add(XL["id"])  # sold out between the add and the checkout
    with pytest.raises(Unavailable) as raised:
        await backend.checkout_handoff(session, await backend.get_cart(session))
    assert XL["id"] in str(raised.value)
    assert forms == [], "no payment URL for a cart that cannot be fulfilled"


async def test_checkout_handoff_reports_a_price_change_and_refreshes_the_line():
    import copy

    from shopping_agent.backend import Unavailable

    from tests.test_medusa_storefront import PRODUCTS, XL, FakeMedusa

    products = copy.deepcopy(PRODUCTS["products"])
    fake = FakeMedusa(products=products)
    forms: list[dict] = []
    backend = _storefront_with_stripe(fake, forms)
    session = ShoppingSessionContext(session_id="s-drift", user_id="priya")
    cart = await backend.add_to_cart(session, XL["id"], 1)
    assert cart.items[0].price == 10.0
    shorts = next(p for p in products if any(v["id"] == XL["id"] for v in p["variants"]))
    variant = next(v for v in shorts["variants"] if v["id"] == XL["id"])
    variant["calculated_price"]["calculated_amount"] = 12  # the merchant moved the price
    with pytest.raises(Unavailable) as raised:
        await backend.checkout_handoff(session, await backend.get_cart(session))
    message = str(raised.value)
    assert XL["id"] in message and "10.00" in message and "12.00" in message
    assert forms == []
    refreshed = await backend.get_cart(session)
    assert [(i.product_id, i.price, i.quantity) for i in refreshed.items] == [(XL["id"], 12.0, 1)]
    handoffs = await backend.checkout_handoff(session, refreshed)
    assert handoffs and forms[0]["line_items[0][price_data][unit_amount]"] == "1200"
