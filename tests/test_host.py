"""The lab host: webhook signature, idempotent order placement, success page."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_medusa.host.app import HostServices, ProcessedEvents, build_app
from commerce_medusa.medusa_client import MedusaClient
from commerce_medusa.medusa_storefront import CustomerDirectory
from commerce_medusa.order_placement import ShippingAddress
from commerce_medusa.settings import LabSettings
from commerce_medusa.stripe_checkout import StripeClient, sign_webhook

FIXTURES = Path(__file__).parent / "fixtures"
CART = json.loads((FIXTURES / "medusa_cart.json").read_text())["cart"]
SHIPPING = json.loads((FIXTURES / "medusa_shipping_options.json").read_text())
SECRET = "whsec_test"


class FakeMedusa:
    def __init__(self) -> None:
        self.completions = 0
        self.completed = False

    async def handler(self, request: httpx.Request) -> httpx.Response:
        """Async, and yielding once per request, so two callers interleave the way they
        do against a real server."""
        import asyncio

        await asyncio.sleep(0)
        return self.answer(request)

    def answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path == f"/store/carts/{CART['id']}" and request.method == "GET":
            return httpx.Response(
                200, json={"cart": dict(CART, completed_at="x" if self.completed else None)}
            )
        if path == f"/store/carts/{CART['id']}" and request.method == "POST":
            return httpx.Response(200, json={"cart": {**CART, **body}})
        if path == "/store/shipping-options":
            return httpx.Response(200, json=SHIPPING)
        if path.endswith("/shipping-methods"):
            return httpx.Response(200, json={"cart": CART})
        if path == "/store/payment-collections":
            return httpx.Response(200, json={"payment_collection": {"id": "pay_col_1"}})
        if path.endswith("/payment-sessions"):
            return httpx.Response(200, json={"payment_collection": {"id": "pay_col_1"}})
        if path.endswith("/complete"):
            self.completions += 1
            self.completed = True
            return httpx.Response(
                200,
                json={
                    "type": "order",
                    "order": {
                        "id": "order_1",
                        "display_id": 11,
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
                            "id": "order_1",
                            "display_id": 11,
                            "total": 28,
                            "currency_code": "usd",
                            "cart_id": CART["id"],
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"message": f"unhandled {request.method} {path}"})


class FakeStripe:
    def __init__(self, payment_status: str = "paid") -> None:
        self.payment_status = payment_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/checkout/sessions/"):
            return httpx.Response(
                200,
                json={
                    "id": "cs_test_1",
                    "payment_status": self.payment_status,
                    "metadata": {"medusa_cart_id": CART["id"], "lab_user_id": "priya"},
                    "customer_details": {"email": "priya@lab.local"},
                },
            )
        return httpx.Response(404, json={"error": {"message": "no"}})


def event(payment_status: str = "paid", event_id: str = "evt_1") -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_1",
                    "payment_status": payment_status,
                    "metadata": {"medusa_cart_id": CART["id"], "lab_user_id": "priya"},
                    "customer_details": {"email": "priya@lab.local"},
                }
            },
        }
    ).encode()


@pytest.fixture
def medusa() -> FakeMedusa:
    return FakeMedusa()


@pytest.fixture
def client(medusa, tmp_path) -> TestClient:
    settings = LabSettings(
        stripe_webhook_secret=SECRET, lab_customer_email="priya@lab.local", customer_id="priya"
    )
    customers = CustomerDirectory(
        {
            "priya": {
                "token": "tok",
                "email": "priya@lab.local",
                "display_name": "Priya",
                "address": ShippingAddress("Priya", "Lab", "1 Main", "Austin", "78701", "US"),
            }
        }
    )
    services = HostServices(
        settings=settings,
        medusa=MedusaClient(
            "http://medusa.test", "pk", transport=httpx.MockTransport(medusa.handler)
        ),
        stripe=StripeClient("sk_test", transport=httpx.MockTransport(FakeStripe().handler)),
        customers=customers,
        events=ProcessedEvents(tmp_path / "events.json"),
    )
    return TestClient(build_app(services))


def test_bad_signature_is_rejected(client, medusa):
    response = client.post(
        "/webhooks/stripe", content=event(), headers={"stripe-signature": "t=1,v1=bad"}
    )
    assert response.status_code == 400
    assert medusa.completions == 0


def test_paid_event_places_the_order_once(client, medusa):
    payload = event()
    headers = {"stripe-signature": sign_webhook(payload, SECRET)}
    first = client.post("/webhooks/stripe", content=payload, headers=headers)
    assert first.status_code == 200 and first.json()["order_id"] == "order_1"
    again = client.post("/webhooks/stripe", content=payload, headers=headers)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    other = event(event_id="evt_2")
    third = client.post(
        "/webhooks/stripe", content=other, headers={"stripe-signature": sign_webhook(other, SECRET)}
    )
    assert third.status_code == 200 and third.json()["order_id"] == "order_1"
    assert medusa.completions == 1, "one cart, one order, however many events"


def test_unpaid_event_is_ignored(client, medusa):
    payload = event(payment_status="unpaid")
    response = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sign_webhook(payload, SECRET)},
    )
    assert response.status_code == 200 and response.json()["ignored"] == "unpaid"
    assert medusa.completions == 0


def test_success_page_places_the_order_when_the_webhook_is_late(client, medusa):
    page = client.get("/checkout/success", params={"session_id": "cs_test_1"})
    assert page.status_code == 200
    assert "Order #11" in page.text
    assert medusa.completions == 1
    page_again = client.get("/checkout/success", params={"session_id": "cs_test_1"})
    assert "Order #11" in page_again.text and medusa.completions == 1


def test_processed_events_survive_a_restart(tmp_path):
    path = tmp_path / "events.json"
    ProcessedEvents(path).record("evt_9", "order_9")
    assert ProcessedEvents(path).seen("evt_9") == "order_9"


def test_mismatched_payment_is_held_not_placed(client, medusa):
    """Paid-amount check: the host completes a paid cart only when Stripe's amount matches the
    cart's total at completion; otherwise the payment is held for review and nothing is placed."""
    short = json.loads(event(event_id="evt_short"))
    short["data"]["object"]["amount_total"] = 1500  # the fixture cart totals 20.00
    payload = json.dumps(short).encode()
    response = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sign_webhook(payload, SECRET)},
    )
    assert response.status_code == 200
    assert response.json()["held"] is True and "1500" in response.json()["reason"]
    assert medusa.completions == 0
    assert client.get("/health").json()["orders_held"] == 1
    exact = json.loads(event(event_id="evt_exact"))
    exact["data"]["object"]["amount_total"] = 2000
    payload = json.dumps(exact).encode()
    response = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sign_webhook(payload, SECRET)},
    )
    assert response.status_code == 200 and response.json()["order_id"] == "order_1"
    assert medusa.completions == 1


async def test_webhook_and_success_page_racing_place_one_order(tmp_path):
    """The webhook and the browser's return page can both try to complete the same paid
    cart at the same moment; the first completes it, the second reads the result."""
    import asyncio

    from commerce_medusa.host.app import HostServices, ProcessedEvents
    from commerce_medusa.medusa_client import MedusaClient
    from commerce_medusa.medusa_storefront import CustomerDirectory
    from commerce_medusa.stripe_checkout import StripeClient

    medusa = FakeMedusa()
    client = MedusaClient("http://medusa.test", "pk", transport=httpx.MockTransport(medusa.handler))
    services = HostServices(
        settings=LabSettings(
            stripe_webhook_secret=SECRET, lab_customer_email="priya@lab.local", customer_id="priya"
        ),
        medusa=client,
        stripe=StripeClient(
            "sk_test", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        ),
        customers=CustomerDirectory({"priya": {"token": "tok", "email": "priya@lab.local"}}),
        events=ProcessedEvents(tmp_path / "events.json"),
    )
    checkout_session = json.loads(event())["data"]["object"]
    first, second = await asyncio.gather(
        services.fulfil(checkout_session), services.fulfil(checkout_session)
    )
    assert first.order_id == second.order_id == "order_1"
    assert medusa.completions == 1, "one cart, one completion, however the two arrive"


def test_any_configured_webhook_secret_verifies(tmp_path):
    """The CLI listener and a real Stripe endpoint sign with different secrets; the host
    accepts either that is configured (STRIPE_WEBHOOK_SECRETS, comma separated)."""
    settings = LabSettings(
        stripe_webhook_secret=SECRET,
        stripe_webhook_secrets="whsec_endpoint_a,whsec_endpoint_b",
        lab_customer_email="priya@lab.local",
        customer_id="priya",
    )
    assert set(settings.webhook_secrets) == {SECRET, "whsec_endpoint_a", "whsec_endpoint_b"}
    medusa = FakeMedusa()
    client = TestClient(
        build_app(
            HostServices(
                settings=settings,
                medusa=MedusaClient(
                    "http://medusa.test", "pk", transport=httpx.MockTransport(medusa.handler)
                ),
                stripe=StripeClient(
                    "sk_test", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
                ),
                customers=CustomerDirectory(
                    {"priya": {"token": "tok", "email": "priya@lab.local"}}
                ),
                events=ProcessedEvents(tmp_path / "events.json"),
            )
        )
    )
    payload = event(event_id="evt_b")
    ok = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sign_webhook(payload, "whsec_endpoint_b")},
    )
    assert ok.status_code == 200 and ok.json()["order_id"] == "order_1"
    bad = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sign_webhook(payload, "whsec_unknown")},
    )
    assert bad.status_code == 400


def test_paid_event_tells_the_host_once_per_placed_order(client, medusa):
    """Order notice: the services call ``on_order_placed`` with the placed order and the Checkout
    Session exactly once per cart, however many events arrive; a failing notice does not
    fail the webhook, because the order is already placed."""
    told: list[tuple[str, str]] = []

    def notice(placed, checkout_session):
        told.append((placed.order_id, checkout_session["metadata"]["medusa_cart_id"]))
        if len(told) == 1:
            raise RuntimeError("the session store hiccupped")

    client.app.state.services.on_order_placed = notice
    payload = event()
    headers = {"stripe-signature": sign_webhook(payload, SECRET)}
    assert client.post("/webhooks/stripe", content=payload, headers=headers).status_code == 200
    other = event(event_id="evt_2")
    client.post(
        "/webhooks/stripe", content=other, headers={"stripe-signature": sign_webhook(other, SECRET)}
    )
    assert told == [("order_1", CART["id"])]
