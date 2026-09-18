"""Cross-role, against the running lab Medusa: a merchant change applied through the
Admin API is what the storefront adapter reads next, and a storefront order shows up
in the merchant snapshot. No model involved; the adapters are driven directly.
Skipped when the server or the lab ``.env`` is absent. Every write is undone."""

from __future__ import annotations

import os

import httpx
import pytest
from merchant_agent import (
    InventoryActionItem,
    MerchantAgentConfig,
    MerchantSessionContext,
    PriceUpdateItem,
)
from shopping_agent import ShoppingSessionContext

from commerce_medusa.medusa_admin import MedusaAdmin
from commerce_medusa.medusa_client import MedusaClient
from commerce_medusa.medusa_merchant import MedusaMerchant
from commerce_medusa.medusa_storefront import CustomerDirectory, MedusaStorefront
from commerce_medusa.settings import LabSettings

TENT_SKU = "AR-1201"


def _reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


settings = LabSettings.load()
pytestmark = pytest.mark.skipif(
    not settings.medusa_admin_email or not _reachable(settings.medusa_url),
    reason="lab Medusa not running or .env incomplete",
)


@pytest.fixture
async def roles():
    client = MedusaClient(settings.medusa_url, settings.medusa_publishable_key)
    customers = CustomerDirectory()
    await customers.login(
        client, settings.customer_id, settings.lab_customer_email, settings.lab_customer_password
    )
    region = await client.first_region_id(settings.lab_currency)
    storefront = MedusaStorefront(
        client,
        region_id=region,
        customers=customers,
        policies_path=settings.data_file("policies.json"),
    )
    admin = MedusaAdmin(client, settings.medusa_admin_email, settings.medusa_admin_password)
    merchant = MedusaMerchant(
        admin,
        config=MerchantAgentConfig(brand_name="Lab Store"),
        currency=settings.lab_currency,
        fixtures_dir=settings.fixtures_dir,
    )
    yield storefront, merchant
    await client.aclose()


@pytest.fixture
def shopper() -> ShoppingSessionContext:
    return ShoppingSessionContext(session_id=f"x-{os.getpid()}", user_id=settings.customer_id)


@pytest.fixture
def operator() -> MerchantSessionContext:
    return MerchantSessionContext(
        session_id=f"xm-{os.getpid()}", merchant_id="lab-store", operator=settings.operator
    )


async def test_merchant_price_change_reaches_the_storefront(roles, shopper, operator):
    storefront, merchant = roles
    tent = (await merchant.search_listings(operator, "2-person backpacking tent"))[0]
    assert TENT_SKU in tent.attributes.get("sku", "") or "Backpacking" in tent.title
    before = tent.price
    new_price = round(before * 1.1, 2)
    change = await merchant.stage_price_update(
        operator, [PriceUpdateItem(listing_id=tent.listing_id, new_price=new_price)]
    )
    try:
        await merchant.apply_change(operator, change.change_id)
        seen = await storefront.get_product_details(shopper, tent.listing_id)
        assert seen is not None and seen.price == new_price
    finally:
        undo = await merchant.stage_price_update(
            operator, [PriceUpdateItem(listing_id=tent.listing_id, new_price=before)]
        )
        await merchant.apply_change(operator, undo.change_id)
    restored = await storefront.get_product_details(shopper, tent.listing_id)
    assert restored is not None and restored.price == before


async def test_merchant_restock_changes_storefront_availability(roles, shopper, operator):
    storefront, merchant = roles
    tent = (await merchant.search_listings(operator, "2-person backpacking tent"))[0]
    before = tent.stock
    change = await merchant.stage_inventory_action(
        operator, [InventoryActionItem(listing_id=tent.listing_id, action="restock", quantity=5)]
    )
    try:
        await merchant.apply_change(operator, change.change_id)
        after = await merchant.get_listing(operator, tent.listing_id)
        assert after is not None and after.stock == before + 5
        seen = await storefront.get_product_details(shopper, tent.listing_id)
        assert seen is not None and seen.in_stock
    finally:
        # A restock only adds, so the undo is a direct level write through the admin.
        await merchant._load()
        variant, _ = merchant._variant_for_write(tent.listing_id)
        for entry in variant.get("inventory_items") or []:
            level = merchant._levels[str(entry["inventory_item_id"])]
            await merchant.admin.post(
                f"/admin/inventory-items/{entry['inventory_item_id']}/location-levels/"
                f"{merchant._location_id}",
                {"stocked_quantity": int(level["stocked_quantity"]) - 5},
            )
    restored = await merchant.get_listing(operator, tent.listing_id)
    assert restored is not None and restored.stock == before


async def test_live_orders_are_in_the_merchant_snapshot(roles, operator):
    _, merchant = roles
    snapshot = await merchant.get_business_snapshot(operator, "last_90_days")
    assert snapshot.orders >= 1
    issues = await merchant.get_order_issues(operator)
    assert isinstance(issues, list)
