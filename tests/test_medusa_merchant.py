"""``MedusaMerchant`` over a mocked Admin API answered from recorded fixtures (the
imported retail catalog with its ``mer:`` metadata, inventory levels, one order).
Writes are checked by what was posted; the store state mutates in the fake so a
follow-up read sees the change."""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from merchant_agent import (
    ChangeKind,
    ChangeStatus,
    InventoryActionItem,
    ListingFilters,
    MerchantAgentConfig,
    MerchantSessionContext,
    PriceUpdateItem,
    PromotionDraft,
)
from merchant_agent.changes import ChangeNotApplicable, GuardrailViolation

from commerce_medusa.medusa_admin import MedusaAdmin
from commerce_medusa.medusa_client import MedusaClient
from commerce_medusa.medusa_merchant import MedusaMerchant
from commerce_medusa.settings import LabSettings

FIXTURES = Path(__file__).parent / "fixtures"
DATA = LabSettings.load().fixtures_dir
PRODUCTS = json.loads((FIXTURES / "medusa_admin_products.json").read_text())["products"]
INVENTORY = json.loads((FIXTURES / "medusa_admin_inventory.json").read_text())["inventory_items"]
ORDERS = json.loads((FIXTURES / "medusa_admin_orders.json").read_text())["orders"]
LOCATION = INVENTORY[0]["location_levels"][0]["location_id"]


def by_handle(handle: str) -> dict:
    return next(p for p in PRODUCTS if p["handle"] == handle)


class FakeAdmin:
    def __init__(self) -> None:
        self.products = {p["id"]: copy.deepcopy(p) for p in PRODUCTS}
        self.inventory = {i["id"]: copy.deepcopy(i) for i in INVENTORY}
        self.orders = copy.deepcopy(ORDERS)
        self.writes: list[tuple[str, str, dict | None]] = []
        self.promotions: list[dict] = []
        self.campaigns: list[dict] = []
        self.fail_after: int | None = None  # writes answer 500 once this many succeeded
        self.campaign_rows: list[dict] = []  # /admin/campaigns

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path, params = request.url.path, dict(request.url.params)
        if path == "/auth/user/emailpass":
            return httpx.Response(200, json={"token": "admin-tok"})
        assert request.headers.get("authorization") == "Bearer admin-tok"
        if request.method == "GET":
            return self._read(path, params)
        if self.fail_after is not None and len(self.writes) >= self.fail_after:
            return httpx.Response(500, json={"message": "platform write refused"})
        self.writes.append((request.method, path, body))
        return self._write(path, body)

    def _page(self, rows: list[dict], params: dict, key: str) -> httpx.Response:
        limit, offset = int(params.get("limit", 50)), int(params.get("offset", 0))
        return httpx.Response(200, json={key: rows[offset : offset + limit], "count": len(rows)})

    def _read(self, path: str, params: dict) -> httpx.Response:
        if path == "/admin/products":
            return self._page(list(self.products.values()), params, "products")
        if path == "/admin/inventory-items":
            return self._page(list(self.inventory.values()), params, "inventory_items")
        if path == "/admin/orders":
            return self._page(self.orders, params, "orders")
        if path == "/admin/campaigns":
            return self._page(list(self.campaign_rows), params, "campaigns")
        return httpx.Response(404, json={"message": f"unhandled GET {path}"})

    def _write(self, path: str, body: dict | None) -> httpx.Response:
        parts = path.strip("/").split("/")
        if parts[:2] == ["admin", "products"] and len(parts) == 3:
            product = self.products[parts[2]]
            for key in ("status", "title", "description", "subtitle", "metadata"):
                if body and key in body:
                    product[key] = body[key]
            return httpx.Response(200, json={"product": product})
        if parts[:2] == ["admin", "products"] and len(parts) == 5 and parts[3] == "variants":
            product = self.products[parts[2]]
            variant = next(v for v in product["variants"] if v["id"] == parts[4])
            if body and "prices" in body:
                variant["prices"] = body["prices"]
            return httpx.Response(200, json={"product": product})
        if parts[:2] == ["admin", "inventory-items"] and "location-levels" in parts:
            item = self.inventory[parts[2]]
            level = next(lv for lv in item["location_levels"] if lv["location_id"] == parts[4])
            level["stocked_quantity"] = body["stocked_quantity"]
            level["available_quantity"] = body["stocked_quantity"] - level.get(
                "reserved_quantity", 0
            )
            return httpx.Response(200, json={"inventory_item": item})
        if parts[:2] == ["admin", "orders"] and len(parts) == 4 and parts[3] == "cancel":
            order = next(o for o in self.orders if o["id"] == parts[2])
            order["status"] = "canceled"
            return httpx.Response(200, json={"order": order})
        if path == "/admin/promotions":
            self.promotions.append(body or {})
            return httpx.Response(200, json={"promotion": {"id": "promo_1", **(body or {})}})
        if path == "/admin/campaigns":
            self.campaigns.append(body or {})
            return httpx.Response(200, json={"campaign": {"id": "procamp_1", **(body or {})}})
        return httpx.Response(404, json={"message": f"unhandled write {path}"})


@pytest.fixture
def fake() -> FakeAdmin:
    return FakeAdmin()


@pytest.fixture
def backend(fake) -> MedusaMerchant:
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    admin = MedusaAdmin(client, email="admin@lab.local", password="pw")
    return MedusaMerchant(
        admin, config=MerchantAgentConfig(brand_name="Lab Store"), fixtures_dir=DATA
    )


@pytest.fixture
def session() -> MerchantSessionContext:
    return MerchantSessionContext(session_id="m-1", merchant_id="lab-store", operator="vishal")


async def test_search_finds_the_wall_decals_with_merchant_facts(backend, session):
    rows = await backend.search_listings(session, "ocean wall decals")
    assert rows
    decals = rows[0]
    assert decals.listing_id == by_handle("ar-2102")["id"]
    assert decals.stock == 3
    assert decals.content_quality == "needs_work"
    assert decals.status == "active"
    assert decals.currency == "USD"
    assert decals.price == 24.0


async def test_search_filters_and_browse(backend, session):
    low = await backend.search_listings(session, "", ListingFilters(max_stock=5), limit=50)
    assert low and all(row.stock <= 5 for row in low)
    needs_work = await backend.search_listings(
        session, "", ListingFilters(content_quality="needs_work"), limit=50
    )
    assert needs_work and all(row.content_quality == "needs_work" for row in needs_work)
    assert all(not row.variant_of for row in low), "search returns families and plain products"


async def test_family_listing_details_carry_variants(backend, session):
    blanket = by_handle("ar-1008")
    details = await backend.get_listing(session, blanket["id"])
    assert details is not None
    assert details.options == {"weight": ["12 lb", "15 lb", "20 lb"]}
    assert len(details.variants) == 3
    assert details.stock == sum(v.stock for v in details.variants)
    assert details.price == min(v.price for v in details.variants)
    twelve = next(v for v in details.variants if v.option_values == {"weight": "12 lb"})
    assert twelve.variant_of == blanket["id"]
    one = await backend.get_listing(session, twelve.listing_id)
    assert one is not None and one.variant_of == blanket["id"] and one.variants == []
    assert await backend.get_listing(session, "prod_missing") is None


async def test_missing_attributes_come_from_metadata(backend, session):
    details = await backend.get_listing(session, by_handle("ar-2102")["id"])
    assert details is not None
    assert details.missing_attributes == ["wall coverage", "material"]


async def test_low_stock_alert_for_the_decals(backend, session):
    alerts = await backend.get_inventory_alerts(session)
    decals = next(a for a in alerts if a.listing_id == by_handle("ar-2102")["id"])
    assert decals.kind == "low_stock"
    assert decals.stock == 3 and decals.threshold == 12
    assert decals.days_of_cover == round(3 / (52 / 30), 1)
    assert decals.storefront_visible is True
    assert alerts[0].kind == "low_stock", "low stock sorts first"


async def test_snapshot_merges_fixture_history_with_live_orders(backend, session):
    snapshot = await backend.get_business_snapshot(session)
    assert snapshot.currency == "USD"
    assert snapshot.sales > 0 and snapshot.orders > 0
    assert snapshot.alerts.low_stock >= 1
    assert snapshot.note and "synthetic" in snapshot.note
    series = await backend.query_metrics(session, "sales", period="last_30_days")
    assert len(series.points) == 30
    weekly = await backend.query_metrics(session, "orders", granularity="week")
    assert weekly.points and weekly.granularity == "week"


async def test_daily_rows_are_calendar_complete_through_today(backend, session):
    await backend._load()
    rows = await backend._daily_rows()
    dates = [date.fromisoformat(r["date"]) for r in rows]
    assert dates[-1] == date.today()
    assert all((b - a).days == 1 for a, b in zip(dates[:-1], dates[1:], strict=True))
    usd = [o for o in ORDERS if o["currency_code"] == "usd"]
    assert usd, "the recorded admin orders include a USD order from the checkout e2e"
    live = next(r for r in rows if r["date"] == usd[0]["created_at"][:10])
    assert live["orders"] >= 1


async def test_pricing_context_uses_unit_cost(backend, session):
    context = await backend.get_pricing_context(session, by_handle("ar-2102")["id"])
    assert context is not None
    assert context.unit_cost == 10.5
    assert context.margin_pct == round((24 - 10.5) / 24 * 100, 1)
    assert context.min_price == round(10.5 * 1.15, 2)
    assert context.demand_signal == "rising"
    family = await backend.get_pricing_context(session, by_handle("ar-1008")["id"])
    assert family is not None and len(family.variants) == 3


async def test_price_update_stages_then_applies_to_the_variant_price(backend, session, fake):
    decals = by_handle("ar-2102")
    change = await backend.stage_price_update(
        session, [PriceUpdateItem(listing_id=decals["id"], new_price=26.0)]
    )
    assert change.status == "staged"
    assert change.items[0].before == 24.0 and change.items[0].after == 26.0
    assert (
        change.margin_before_pct is not None and change.margin_after_pct > change.margin_before_pct
    )
    assert not fake.writes, "staging writes nothing"
    applied = await backend.apply_change(session, change.change_id)
    assert applied.status == "applied" and applied.applied_by == "vishal"
    variant_write = next(w for w in fake.writes if "/variants/" in w[1])
    assert {"currency_code": "usd", "amount": 26.0} in variant_write[2]["prices"]
    listing = await backend.get_listing(session, decals["id"])
    assert listing is not None and listing.price == 26.0


async def test_price_move_over_the_cap_is_refused(backend, session):
    decals = by_handle("ar-2102")
    # 24 -> 31 is inside the backend ceiling (24 * 1.35) but over the 20% guardrail.
    with pytest.raises(GuardrailViolation):
        await backend.stage_price_update(
            session, [PriceUpdateItem(listing_id=decals["id"], new_price=31.0)]
        )


async def test_price_outside_the_backend_range_is_refused_first(backend, session):
    decals = by_handle("ar-2102")
    with pytest.raises(ChangeNotApplicable):
        await backend.stage_price_update(
            session, [PriceUpdateItem(listing_id=decals["id"], new_price=40.0)]
        )
    with pytest.raises(ChangeNotApplicable):
        await backend.stage_price_update(
            session, [PriceUpdateItem(listing_id=decals["id"], new_price=5.0)]
        )


async def test_price_update_on_a_family_is_refused(backend, session):
    with pytest.raises(ValueError):
        await backend.stage_price_update(
            session, [PriceUpdateItem(listing_id=by_handle("ar-1008")["id"], new_price=50.0)]
        )


async def test_restock_applies_to_the_inventory_level(backend, session, fake):
    decals = by_handle("ar-2102")
    change = await backend.stage_inventory_action(
        session, [InventoryActionItem(listing_id=decals["id"], action="restock", quantity=60)]
    )
    assert change.items[0].before == 3 and change.items[0].after == 63
    await backend.apply_change(session, change.change_id)
    level_write = next(w for w in fake.writes if "location-levels" in w[1])
    assert level_write[2] == {"stocked_quantity": 63}
    assert level_write[1].endswith(f"/location-levels/{LOCATION}")
    listing = await backend.get_listing(session, decals["id"])
    assert listing is not None and listing.stock == 63
    alerts = await backend.get_inventory_alerts(session)
    assert all(a.listing_id != decals["id"] or a.kind != "low_stock" for a in alerts)


async def test_pause_sets_the_product_to_draft(backend, session, fake):
    tent = by_handle("ar-1201")
    change = await backend.stage_inventory_action(
        session, [InventoryActionItem(listing_id=tent["id"], action="pause")]
    )
    await backend.apply_change(session, change.change_id)
    assert ("POST", f"/admin/products/{tent['id']}", {"status": "draft"}) in fake.writes
    listing = await backend.get_listing(session, tent["id"])
    assert listing is not None and listing.status == "paused"


async def test_promotion_on_a_family_expands_to_variants_and_creates_a_medusa_promotion(
    backend, session, fake
):
    blanket = by_handle("ar-1008")
    today = date.today()
    draft = PromotionDraft(
        name="Weekend blanket sale",
        listing_ids=[blanket["id"]],
        discount_pct=15,
        starts=today.isoformat(),
        ends=(today + timedelta(days=2)).isoformat(),
    )
    change = await backend.stage_promotion(session, draft)
    assert len(change.items) == 3
    assert all(item.field == "promotion_price" for item in change.items)
    assert change.items[0].after == round(change.items[0].before * 0.85, 2)
    await backend.apply_change(session, change.change_id)
    assert len(fake.promotions) == 1
    promo = fake.promotions[0]
    assert promo["application_method"]["value"] == 15.0
    assert promo["application_method"]["target_rules"][0]["values"] == [blanket["id"]]


async def test_listing_update_writes_title_and_attributes(backend, session, fake):
    decals = by_handle("ar-2102")
    change = await backend.stage_listing_update(
        session, decals["id"], {"title": "Ocean Wall Decals, 40-piece set", "material": "vinyl"}
    )
    await backend.apply_change(session, change.change_id)
    write = next(w for w in fake.writes if w[1] == f"/admin/products/{decals['id']}")
    assert write[2]["title"] == "Ocean Wall Decals, 40-piece set"
    assert write[2]["metadata"]["attr:material"] == "vinyl"


async def test_apply_and_discard_lifecycle(backend, session):
    decals = by_handle("ar-2102")
    change = await backend.stage_price_update(
        session, [PriceUpdateItem(listing_id=decals["id"], new_price=25.0)]
    )
    assert [c.change_id for c in await backend.get_pending_changes(session)] == [change.change_id]
    discarded = await backend.discard_change(session, change.change_id)
    assert discarded.status == "discarded"
    with pytest.raises(ChangeNotApplicable):
        await backend.apply_change(session, change.change_id)
    with pytest.raises(ChangeNotApplicable):
        await backend.apply_change(session, "chg-9999")


async def test_merchant_context_names_limitations(backend, session):
    context = await backend.get_merchant_context(session)
    assert context is not None
    assert context["catalog_size"] == len(PRODUCTS)
    assert {row["source"] for row in context["limitations"]} == {"traffic", "campaigns"}
    assert context["alerts"]["low_stock"] >= 1


# -- idempotent apply --------------------------------------------------------------


async def test_interrupted_apply_is_completed_by_a_retry_without_a_second_write(fake, tmp_path):
    """A crash after the platform write but before the ledger stamp must not restock twice:
    the retry sees the item's progress record and only stamps."""
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    admin = MedusaAdmin(client, email="admin@lab.local", password="pw")
    backend = MedusaMerchant(
        admin,
        config=MerchantAgentConfig(brand_name="Lab Store"),
        fixtures_dir=DATA,
        ledger_path=tmp_path / "ledger.sqlite",
    )
    session = MerchantSessionContext(session_id="m-9", merchant_id="lab-store", operator="v")
    decals = by_handle("ar-2102")
    before = (await backend.get_listing(session, decals["id"])).stock
    change = await backend.stage_inventory_action(
        session, [InventoryActionItem(listing_id=decals["id"], action="restock", quantity=10)]
    )
    backend.interrupt_before_stamp = True  # test hook: the process dies after the write
    with pytest.raises(RuntimeError):
        await backend.apply_change(session, change.change_id)
    backend.interrupt_before_stamp = False
    assert backend.ledger.get(change.change_id).status is ChangeStatus.STAGED
    restock_writes = [w for w in fake.writes if "location-levels" in w[1]]
    assert len(restock_writes) == 1
    # A new process over the same ledger and store retries.
    again = MedusaMerchant(
        admin,
        config=MerchantAgentConfig(brand_name="Lab Store"),
        fixtures_dir=DATA,
        ledger_path=tmp_path / "ledger.sqlite",
    )
    applied = await again.apply_change(session, change.change_id)
    assert applied.status is ChangeStatus.APPLIED
    assert len([w for w in fake.writes if "location-levels" in w[1]]) == 1, "no second restock"
    assert (await again.get_listing(session, decals["id"])).stock == before + 10


async def test_a_claimed_change_cannot_be_applied_by_another_process(fake, tmp_path):
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    admin = MedusaAdmin(client, email="admin@lab.local", password="pw")
    make = lambda: MedusaMerchant(  # noqa: E731
        admin,
        config=MerchantAgentConfig(brand_name="Lab Store"),
        fixtures_dir=DATA,
        ledger_path=tmp_path / "ledger.sqlite",
    )
    one, two = make(), make()
    session = MerchantSessionContext(session_id="m-10", merchant_id="lab-store", operator="v")
    change = await one.stage_inventory_action(
        session, [InventoryActionItem(listing_id=by_handle("ar-1001")["id"], action="pause")]
    )
    assert [c.change_id for c in await two.get_pending_changes(session)] == [change.change_id]
    assert one.ledger.claim(change.change_id, "host-one")
    with pytest.raises(ChangeNotApplicable):
        await two.apply_change(session, change.change_id)
    one.ledger.release(change.change_id)
    applied = await two.apply_change(session, change.change_id)
    assert applied.status is ChangeStatus.APPLIED
    assert (await one.get_listing(session, by_handle("ar-1001")["id"])).status == "paused"


# -- order requests: the shopper's requests reach the merchant -------------------------------------


async def test_open_requests_are_order_issues_and_an_approved_cancel_cancels_the_order(
    fake, tmp_path
):
    from commerce_medusa.order_requests import OrderRequestStore

    store = OrderRequestStore(tmp_path / "requests.sqlite")
    order = ORDERS[0]
    request = store.create(
        user_id="priya", order_id=order["id"], action="cancel", item_ids=[], reason="wrong size"
    )
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    backend = MedusaMerchant(
        MedusaAdmin(client, email="admin@lab.local", password="pw"),
        config=MerchantAgentConfig(brand_name="Lab Store"),
        fixtures_dir=DATA,
        requests=store,
    )
    session = MerchantSessionContext(session_id="m-r", merchant_id="lab-store", operator="vishal")
    issues = await backend.get_order_issues(session)
    mine = next(i for i in issues if i.issue_id == request.request_id)
    assert mine.kind == "buyer_message" and mine.order_id == order["id"]
    assert "cancel" in mine.summary.lower() and mine.buyer_message_excerpt == "wrong size"
    resolved = await backend.resolve_order_request(session, request.request_id, "approved", "ok")
    assert resolved.status == "approved" and resolved.resolved_by == "vishal"
    assert ("POST", f"/admin/orders/{order['id']}/cancel", {}) in fake.writes
    assert all(i.issue_id != request.request_id for i in await backend.get_order_issues(session))
    with pytest.raises(ChangeNotApplicable):
        await backend.resolve_order_request(session, request.request_id, "declined", "twice")


# -- freshness: a stale change is refused at apply ----------------------------------------------


async def test_apply_refuses_a_price_change_staged_against_a_price_that_moved(
    backend, session, fake
):
    decals = by_handle("ar-2102")
    listing = await backend.get_listing(session, decals["id"])
    change = await backend.stage_price_update(
        session,
        [PriceUpdateItem(listing_id=decals["id"], new_price=round(listing.price * 1.05, 2))],
    )
    # Someone else moved the price on the platform after the change was staged.
    variant = decals["variants"][0]
    for price in fake.products[decals["id"]]["variants"][0]["prices"]:
        if price["currency_code"] == "usd":
            price["amount"] = round(listing.price * 1.10, 2)
    with pytest.raises(ChangeNotApplicable, match="moved"):
        await backend.apply_change(session, change.change_id)
    assert backend.ledger.get(change.change_id).status is ChangeStatus.STAGED
    assert variant["id"] not in [w[1] for w in fake.writes if "/variants/" in w[1]], "not written"


async def test_restock_applies_its_delta_to_the_current_stock(backend, session, fake):
    decals = by_handle("ar-2102")
    before = (await backend.get_listing(session, decals["id"])).stock
    change = await backend.stage_inventory_action(
        session, [InventoryActionItem(listing_id=decals["id"], action="restock", quantity=10)]
    )
    # Two units sold on the platform meanwhile.
    item_id = decals["variants"][0]["inventory_items"][0]["inventory_item_id"]
    level = fake.inventory[item_id]["location_levels"][0]
    level["stocked_quantity"] -= 2
    level["available_quantity"] = level["stocked_quantity"]
    applied = await backend.apply_change(session, change.change_id)
    assert applied.status is ChangeStatus.APPLIED
    assert (await backend.get_listing(session, decals["id"])).stock == before - 2 + 10
    assert any("stock" in n and "now" in n for n in applied.guardrail_notes), (
        applied.guardrail_notes
    )


# -- undo and scheduled apply --------------------------------------------------------


async def test_undo_stages_the_inverse_and_applying_it_restores(backend, session, fake):
    decals = by_handle("ar-2102")
    original = (await backend.get_listing(session, decals["id"])).price
    change = await backend.stage_price_update(
        session, [PriceUpdateItem(listing_id=decals["id"], new_price=round(original * 1.05, 2))]
    )
    await backend.apply_change(session, change.change_id)
    assert (await backend.get_listing(session, decals["id"])).price == round(original * 1.05, 2)
    inverse = await backend.undo_change(session, change.change_id)
    assert inverse.status is ChangeStatus.STAGED and inverse.kind is ChangeKind.PRICE_UPDATE
    assert (
        inverse.items[0].before == round(original * 1.05, 2) and inverse.items[0].after == original
    )
    assert change.change_id in inverse.summary
    await backend.apply_change(session, inverse.change_id)
    assert (await backend.get_listing(session, decals["id"])).price == original
    with pytest.raises(ChangeNotApplicable):
        await backend.undo_change(session, change.change_id)  # undone already
    with pytest.raises(ChangeNotApplicable):
        await backend.undo_change(session, "chg-nope")


async def test_promotion_cannot_be_undone(backend, session):
    decals = by_handle("ar-2102")
    today = date.today()
    change = await backend.stage_promotion(
        session,
        PromotionDraft(
            name="Weekend",
            listing_ids=[decals["id"]],
            discount_pct=10,
            starts=today.isoformat(),
            ends=(today + timedelta(days=2)).isoformat(),
        ),
    )
    await backend.apply_change(session, change.change_id)
    with pytest.raises(ChangeNotApplicable, match="cannot be undone"):
        await backend.undo_change(session, change.change_id)


async def test_scheduled_changes_apply_when_due(backend, session, fake):
    from datetime import UTC, datetime, timedelta

    decals = by_handle("ar-2102")
    before = (await backend.get_listing(session, decals["id"])).stock
    change = await backend.stage_inventory_action(
        session, [InventoryActionItem(listing_id=decals["id"], action="restock", quantity=4)]
    )
    now = datetime.now(UTC)
    scheduled = await backend.schedule_change(session, change.change_id, now + timedelta(hours=1))
    assert scheduled.change_id == change.change_id
    assert await backend.apply_due_changes(session, now) == []
    assert (await backend.get_listing(session, decals["id"])).stock == before
    applied = await backend.apply_due_changes(session, now + timedelta(hours=2))
    assert [c.change_id for c in applied] == [change.change_id]
    assert applied[0].applied_by == session.operator
    assert (await backend.get_listing(session, decals["id"])).stock == before + 4
    assert await backend.apply_due_changes(session, now + timedelta(hours=3)) == []


# -- campaign performance from the platform ------------------------------------------


async def test_campaign_performance_merges_platform_campaigns(fake, session):
    fake.campaign_rows = [
        {
            "id": "procamp_live_1",
            "name": "Autumn bundle",
            "campaign_identifier": "autumn-bundle",
            "starts_at": "2026-09-01T00:00:00Z",
            "ends_at": "2026-09-30T00:00:00Z",
            "budget": {"type": "spend", "limit": 500, "used": 120, "currency_code": "usd"},
        }
    ]
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    backend = MedusaMerchant(
        MedusaAdmin(client, email="admin@lab.local", password="pw"),
        config=MerchantAgentConfig(brand_name="Lab Store"),
        fixtures_dir=DATA,
    )
    campaigns = await backend.get_campaign_performance(session)
    live = next(c for c in campaigns if c.campaign_id == "procamp_live_1")
    assert live.name == "Autumn bundle" and live.budget == 500 and live.spend == 120
    assert live.revenue is None, "the platform reports spend against the budget, not revenue"
    assert live.status == "active" and live.channel == "medusa"
    assert len(await backend.get_campaign_performance(session, "procamp_live_1")) == 1
    assert any(c.campaign_id != "procamp_live_1" for c in campaigns), "fixture campaigns remain"


# -- the waitlist reaches the merchant -------------------------------------------------


async def test_merchant_context_carries_waitlist_counts(fake, tmp_path):
    from commerce_medusa.order_requests import WaitlistStore

    waitlist = WaitlistStore(tmp_path / "w.sqlite")
    decals = by_handle("ar-2102")
    waitlist.subscribe(user_id="priya", product_id=decals["variants"][0]["id"])
    waitlist.subscribe(user_id="sam", product_id=decals["variants"][0]["id"])
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    backend = MedusaMerchant(
        MedusaAdmin(client, email="admin@lab.local", password="pw"),
        config=MerchantAgentConfig(brand_name="Lab Store"),
        fixtures_dir=DATA,
        waitlist=waitlist,
    )
    session = MerchantSessionContext(session_id="m-w", merchant_id="lab-store", operator="v")
    context = await backend.get_merchant_context(session)
    assert context["waitlist"] == {decals["id"]: 2}, "counted per listing, by product"
