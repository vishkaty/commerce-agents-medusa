"""Stripe checkout helpers: form encoding, session creation body, signature check."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
from shopping_agent import Cart, CartItem

from commerce_medusa.stripe_checkout import (
    CheckoutLine,
    StripeClient,
    StripeError,
    cents,
    checkout_lines,
    form_encode,
    sign_webhook,
    verify_webhook_signature,
)


def test_form_encoding_uses_stripe_brackets():
    encoded = form_encode(
        {
            "mode": "payment",
            "line_items": [{"quantity": 2, "price_data": {"currency": "usd"}}],
            "metadata": {"a": "b"},
        }
    )
    assert encoded == {
        "mode": "payment",
        "line_items[0][quantity]": "2",
        "line_items[0][price_data][currency]": "usd",
        "metadata[a]": "b",
    }


def test_cents_rounds_money():
    assert cents(149.0) == 14900
    assert cents(8.005) == 801 or cents(8.005) == 800  # float rounding, never a crash
    assert cents(0.1 + 0.2) == 30


def test_checkout_lines_add_shipping_when_charged():
    cart = Cart(
        items=[
            CartItem(product_id="v1", title="Tent", price=149.0, quantity=1),
            CartItem(product_id="v2", title="Lamp", price=34.0, quantity=2),
        ],
        currency="USD",
    )
    lines = checkout_lines(cart, "Standard Shipping (US)", 8.0)
    assert lines == [
        CheckoutLine("Tent", 14900, 1),
        CheckoutLine("Lamp", 3400, 2),
        CheckoutLine("Standard Shipping (US)", 800, 1),
    ]
    assert checkout_lines(cart, None, 0.0)[-1].name == "Lamp"


async def test_create_checkout_session_posts_the_right_form():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["form"] = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        return httpx.Response(
            200,
            json={
                "id": "cs_test_1",
                "url": "https://checkout.stripe.com/c/pay/cs_test_1",
                "status": "open",
            },
        )

    client = StripeClient("sk_test_x", transport=httpx.MockTransport(handler))
    session = await client.create_checkout_session(
        lines=[CheckoutLine("Tent", 14900, 1)],
        currency="USD",
        success_url="http://localhost:8010/checkout/success?session_id={CHECKOUT_SESSION_ID}",
        cancel_url="http://localhost:8010/checkout/cancel",
        metadata={"medusa_cart_id": "cart_1", "lab_session_id": "s-1"},
        customer_email="priya@lab.local",
    )
    assert session["id"] == "cs_test_1"
    assert seen["path"] == "/v1/checkout/sessions"
    assert seen["auth"].startswith("Basic ")
    form = seen["form"]
    assert form["mode"] == "payment"
    assert form["line_items[0][price_data][currency]"] == "usd"
    assert form["line_items[0][price_data][unit_amount]"] == "14900"
    assert form["line_items[0][price_data][product_data][name]"] == "Tent"
    assert form["metadata[medusa_cart_id]"] == "cart_1"
    assert form["client_reference_id"] == "cart_1"
    assert form["customer_email"] == "priya@lab.local"


async def test_stripe_errors_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "card declined"}})

    client = StripeClient("sk_test_x", transport=httpx.MockTransport(handler))
    with pytest.raises(StripeError) as raised:
        await client.retrieve_checkout_session("cs_x")
    assert raised.value.status == 402


def test_webhook_signature_round_trip():
    payload = json.dumps({"id": "evt_1", "type": "checkout.session.completed"}).encode()
    header = sign_webhook(payload, "whsec_test", timestamp=1_700_000_000)
    assert verify_webhook_signature(payload, header, "whsec_test", now=1_700_000_100)
    assert not verify_webhook_signature(payload, header, "whsec_other", now=1_700_000_100)
    assert not verify_webhook_signature(payload + b" ", header, "whsec_test", now=1_700_000_100)
    assert not verify_webhook_signature(payload, header, "whsec_test", now=1_700_001_000), "stale"
    assert not verify_webhook_signature(payload, "garbage", "whsec_test")
