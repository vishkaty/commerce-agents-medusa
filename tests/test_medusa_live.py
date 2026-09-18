"""Integration: the adapter against the running lab Medusa (``make medusa-dev``). Skipped
when the server or the lab ``.env`` is absent, so the offline suite stays green."""

from __future__ import annotations

import os

import httpx
import pytest
from shopping_agent import ShoppingSessionContext

from commerce_medusa.medusa_client import MedusaClient
from commerce_medusa.medusa_storefront import CustomerDirectory, MedusaStorefront
from commerce_medusa.settings import LabSettings


def _reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


settings = LabSettings.load()
pytestmark = pytest.mark.skipif(
    not settings.medusa_publishable_key or not _reachable(settings.medusa_url),
    reason="lab Medusa not running or .env incomplete",
)


@pytest.fixture
async def backend() -> MedusaStorefront:
    client = MedusaClient(settings.medusa_url, settings.medusa_publishable_key)
    customers = CustomerDirectory()
    await customers.login(
        client, settings.customer_id, settings.lab_customer_email, settings.lab_customer_password
    )
    region = await client.first_region_id(settings.lab_currency)
    return MedusaStorefront(
        client,
        region_id=region,
        customers=customers,
        policies_path=settings.data_file("policies.json"),
    )


@pytest.fixture
def session() -> ShoppingSessionContext:
    return ShoppingSessionContext(session_id=f"live-{os.getpid()}", user_id=settings.customer_id)


async def test_search_details_cart_orders_round_trip(backend, session):
    results = await backend.search_products(session, "shirt")
    assert results, "the seeded catalog has shirts"
    family = results[0]
    details = await backend.get_product_details(session, family.product_id)
    assert details is not None and details.variants
    variant = next(v for v in details.variants if v.in_stock)
    cart = await backend.add_to_cart(session, variant.product_id, 1)
    assert cart.items[0].product_id == variant.product_id
    cart = await backend.update_cart_item(session, variant.product_id, 2)
    assert cart.items[0].quantity == 2
    cart = await backend.remove_from_cart(session, variant.product_id)
    assert cart.items == []
    orders = await backend.get_orders(session)
    assert orders, "order #1 was placed during setup"
    options = await backend.get_fulfillment_options(session, [variant.product_id])
    assert options
    prefs = await backend.get_preferences(session)
    assert prefs.display_name
