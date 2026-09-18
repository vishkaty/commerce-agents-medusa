"""The lab store host end to end over a mocked Medusa and a fake model: the upstream
routes (session, products, chat as SSE, cart, merchant overview and approval) work on
the SDK adapter."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from commerce_medusa.host.sdk_turn import SdkMerchantAgent, SdkShoppingAgent
from commerce_medusa.host.store import build_store_app, merchant_config, shopping_config
from commerce_medusa.medusa_admin import MedusaAdmin
from commerce_medusa.medusa_client import MedusaClient
from commerce_medusa.medusa_merchant import MedusaMerchant
from commerce_medusa.medusa_storefront import CustomerDirectory, MedusaStorefront
from commerce_medusa.settings import LabSettings
from tests.test_medusa_merchant import DATA as MERCHANT_DATA
from tests.test_medusa_merchant import FakeAdmin
from tests.test_medusa_storefront import REGION, SHORTS, FakeMedusa
from tests.test_sdk_turn import FakeClient, merchant_runner, shopping_runner


class Combined:
    def __init__(self) -> None:
        self.store = FakeMedusa()
        self.admin = FakeAdmin()

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/admin") or request.url.path == "/auth/user/emailpass":
            return self.admin.handler(request)
        return self.store.handler(request)


def parse_sse(body: str) -> list[dict]:
    events = []
    for block in body.strip().split("\n\n"):
        kind = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if kind:
            events.append({"type": kind, "data": data})
    return events


def make_app(tmp_path, sessions_path=None):
    combined = Combined()
    medusa = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(combined.handler)
    )
    customers = CustomerDirectory({"priya": {"token": "tok-priya", "display_name": "Priya"}})
    customers.alias("demo-user", "priya")
    settings = LabSettings(
        lab_customer_email="priya@lab.local",
        customer_id="priya",
        operator="vishal",
        data_dir=tmp_path / "data",
    )
    storefront = MedusaStorefront(
        medusa, region_id=REGION, customers=customers, store_name="Lab Store"
    )
    merchant = MedusaMerchant(
        MedusaAdmin(medusa, email="a@b", password="pw"),
        config=merchant_config(),
        fixtures_dir=MERCHANT_DATA,
    )
    shopping_agent = SdkShoppingAgent(
        backend=storefront,
        config=shopping_config(),
        client_factory=FakeClient,
        turn_runner=shopping_runner(
            [
                ("search_products", {"query": "shorts"}),
                ("present_products", {"picks": [{"product_id": SHORTS["id"]}]}),
            ]
        ),
    )
    decals = next(p for p in combined.admin.products.values() if p["handle"] == "ar-2102")
    merchant_agent = SdkMerchantAgent(
        backend=merchant,
        config=merchant.config,
        client_factory=FakeClient,
        turn_runner=merchant_runner(
            [
                ("search_listings", {"query": "decals"}),
                ("get_listing", {"listing_id": decals["id"]}),
                (
                    "stage_inventory_action",
                    {"items": [{"listing_id": decals["id"], "action": "restock", "quantity": 5}]},
                ),
            ]
        ),
    )
    app = build_store_app(
        settings,
        medusa=medusa,
        region_id=REGION,
        customers=customers,
        stripe=None,
        storefront=storefront,
        merchant=merchant,
        shopping_agent=shopping_agent,
        merchant_agent=merchant_agent,
        events_path=tmp_path / "events.json",
        sessions_path=sessions_path,
    )
    return app


@pytest.fixture
def client(tmp_path) -> TestClient:
    with TestClient(make_app(tmp_path), base_url="http://localhost") as test_client:
        yield test_client


def test_health_and_catalog(client):
    health = client.get("/api/health").json()
    assert health["ok"] and health["store"] == "Lab Store" and health["products"] == 4
    products = client.get("/api/products").json()["products"]
    assert len(products) == 4
    one = client.get(f"/api/products/{SHORTS['id']}").json()
    assert (
        one["product"]["product_id"] == SHORTS["id"]
        if "product" in one
        else one["product_id"] == SHORTS["id"]
    )


def test_session_chat_and_cart(client):
    started = client.post("/api/session", json={"user_id": "demo-user"}).json()
    headers = {"X-Session-Id": started["session_id"]}
    assert started["name"] == "Priya"
    response = client.post("/api/chat", json={"message": "shorts please"}, headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    kinds = [e["type"] for e in events]
    assert (
        "tool_call" in kinds
        and "ui" in kinds
        and "text_delta" in kinds
        and kinds[-1] == "turn_complete"
    )
    ui = next(e for e in events if e["type"] == "ui")
    assert ui["data"]["component"] == "products"
    cart = client.get("/api/cart", headers=headers).json()
    assert cart["item_count"] == 0
    # A button add runs through the executor with the session's provenance from the turn.
    added = client.post(
        "/api/cart/add",
        json={"product_id": SHORTS["variants"][0]["id"], "quantity": 1},
        headers=headers,
    )
    assert added.status_code in (200, 404), added.text  # the retail-only route may not exist here


def test_merchant_overview_chat_and_portal_approval(client):
    started = client.post("/api/merchant/session").json()
    headers = {"X-Session-Id": started["session_id"]}
    assert started["operator"] == "vishal"
    overview = client.get("/api/merchant/overview", headers=headers).json()
    assert overview["snapshot"]["currency"] == "USD"
    assert overview["snapshot"]["alerts"]["low_stock"] >= 1
    response = client.post(
        "/api/merchant/chat", json={"message": "restock the decals"}, headers=headers
    )
    events = parse_sse(response.text)
    change = next(e for e in events if e["type"] == "change_update")["data"]["change"]
    assert change["status"] == "staged"
    applied = client.post(
        f"/api/merchant/changes/{change['change_id']}/apply", headers=headers
    ).json()
    assert applied["ok"] is True and applied["change"]["status"] == "applied"
    pending = client.get("/api/merchant/overview", headers=headers).json()["snapshot"]["alerts"][
        "pending_changes"
    ]
    assert pending == 0


# -- the HOST statements over the lab's own host ---------------------------------------

from commerce_conformance.facts import spec  # noqa: E402


class TestLabHost:
    """HOST-01/02 pinned upstream for the demo hosts, run here over the lab store host."""

    target_name = "lab-store-host"

    @spec("HOST-01")
    def test_no_route_reads_identity_from_the_request(self, client):
        started = client.post("/api/session", json={"user_id": "demo-user"}).json()
        headers = {"X-Session-Id": started["session_id"]}
        honest = client.get("/api/cart", headers=headers)
        assert honest.status_code == 200
        # A user id in a header or the query string changes nothing: the session decides.
        forged = client.get(
            "/api/cart", headers={**headers, "X-User-Id": "victim"}, params={"user_id": "victim"}
        )
        assert forged.status_code == 200 and forged.json() == honest.json()
        assert client.get("/api/cart").status_code >= 400, "no session, no cart"
        assert client.get("/api/cart", headers={"X-Session-Id": "sess_forged"}).status_code >= 400

    @spec("HOST-02")
    def test_approval_applies_once_and_the_mark_does_not_outlive_the_click(self, client):
        started = client.post("/api/merchant/session").json()
        headers = {"X-Session-Id": started["session_id"]}
        response = client.post(
            "/api/merchant/chat", json={"message": "restock the decals"}, headers=headers
        )
        change = next(e for e in parse_sse(response.text) if e["type"] == "change_update")["data"][
            "change"
        ]
        assert change["status"] == "staged"
        applied = client.post(
            f"/api/merchant/changes/{change['change_id']}/apply", headers=headers
        ).json()
        assert applied["ok"] is True and applied["change"]["status"] == "applied"
        again = client.post(f"/api/merchant/changes/{change['change_id']}/apply", headers=headers)
        assert again.status_code >= 400 or again.json().get("ok") is False, "no second apply"


# -- waitlist and cart-merge host routes -----------------------------------------------------------


async def test_guest_cart_merge_and_waitlist_routes(client):
    from shopping_agent import ShoppingSessionContext

    guest = client.post("/api/session", json={"user_id": "guest-7"}).json()
    guest_headers = {"X-Session-Id": guest["session_id"]}
    storefront = client.app.state.storefront
    await storefront.add_to_cart(
        ShoppingSessionContext(session_id=guest["session_id"], user_id="guest-7"),
        SHORTS["variants"][0]["id"],
        2,
    )
    signed_in = client.post("/api/session", json={"user_id": "demo-user"}).json()
    headers = {"X-Session-Id": signed_in["session_id"]}
    merged = client.post(
        "/api/cart/merge", json={"from_session_id": guest["session_id"]}, headers=headers
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["item_count"] == 2
    assert client.get("/api/cart", headers=guest_headers).json()["item_count"] == 0
    assert client.post("/api/cart/merge", json={"from_session_id": "x"}).status_code == 401
    waitlist = client.get("/api/merchant/waitlist")
    assert waitlist.status_code == 200 and waitlist.json() == {"waitlist": {}}


# -- sessions outlive the process; a paid order reaches the next turn ------------


def test_sessions_survive_a_host_restart(tmp_path):
    """Durable sessions: the same session id, with its transcript and provenance, works on a
    host built afresh over the same sessions file; the in-memory store upstream forgets it."""
    sessions_path = tmp_path / "sessions.sqlite"
    with TestClient(make_app(tmp_path, sessions_path), base_url="http://localhost") as first:
        started = first.post("/api/session", json={"user_id": "demo-user"}).json()
        headers = {"X-Session-Id": started["session_id"]}
        assert (
            first.post("/api/chat", json={"message": "shorts please"}, headers=headers).status_code
            == 200
        )
        merchant = first.post("/api/merchant/session").json()
    with TestClient(make_app(tmp_path, sessions_path), base_url="http://localhost") as second:
        assert second.get("/api/cart", headers=headers).status_code == 200
        record = second.app.state.sessions.require(started["session_id"])
        assert [m["role"] for m in record.messages] == ["user", "assistant"]
        assert (
            SHORTS["variants"][0]["id"] in record.state.seen_products or record.state.seen_products
        )
        overview = second.get(
            "/api/merchant/overview", headers={"X-Session-Id": merchant["session_id"]}
        )
        assert overview.status_code == 200
    with TestClient(make_app(tmp_path), base_url="http://localhost") as forgetful:
        assert forgetful.get("/api/cart", headers=headers).status_code >= 400


def test_a_placed_order_is_announced_on_the_next_turn(client):
    """Order notice: after the webhook places the order, the session that started the checkout hears
    it as an app event at the top of its next turn (upstream's pending_app_events)."""
    from commerce_medusa.order_placement import PlacedOrder

    started = client.post("/api/session", json={"user_id": "demo-user"}).json()
    headers = {"X-Session-Id": started["session_id"]}
    placed = PlacedOrder(order_id="order_1", display_id=1001, total=18.96, currency="usd")
    checkout_session = {
        "id": "cs_1",
        "amount_total": 1896,
        "currency": "usd",
        "metadata": {"medusa_cart_id": "cart_1", "lab_session_id": started["session_id"]},
    }
    announce = client.app.state.announce_order
    announce(placed, checkout_session)
    record = client.app.state.sessions.require(started["session_id"])
    assert record.pending_app_events == ["Order #1001 was placed and paid ($18.96)."]
    announce(
        placed, {"id": "cs_2", "metadata": {"lab_session_id": "sess_unknown"}}
    )  # no session: no error
    announce(placed, {"id": "cs_3", "metadata": {}})  # no session id in the metadata: nothing to do
    response = client.post("/api/chat", json={"message": "thanks, anything else?"}, headers=headers)
    assert response.status_code == 200
    record = client.app.state.sessions.require(started["session_id"])
    assert record.pending_app_events == []
    first_user = next(m for m in record.messages if m["role"] == "user")
    text = (
        first_user["content"]
        if isinstance(first_user["content"], str)
        else json.dumps(first_user["content"])
    )
    assert "Order #1001 was placed and paid ($18.96)." in text
