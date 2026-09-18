"""Seed the live lab store with what the conformance facts need beyond the catalog:
a second customer with one order of their own, so "another customer's
order" is a real id, and the out-of-stock variant the retail fixture has (AR-1606 king
blush at zero). Idempotent: rerunning changes nothing that already holds.

    .venv/bin/python scripts/seed_lab_customers.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from commerce_medusa.medusa_admin import MedusaAdmin  # noqa: E402
from commerce_medusa.medusa_client import MedusaClient, MedusaError  # noqa: E402
from commerce_medusa.order_placement import ShippingAddress, place_order  # noqa: E402
from commerce_medusa.settings import LabSettings  # noqa: E402

SECOND = {"email": "sam@lab.local", "password": "lab-pass-123", "first": "Sam", "last": "Lab"}
ADDRESS = ShippingAddress("Sam", "Lab", "2 Side St", "Austin", "78702", "US", "tx")
OOS_HANDLE, OOS_VARIANT_TITLE = "ar-1606", "king, blush"


async def second_customer_order(client: MedusaClient, region: str) -> str:
    try:
        token = await client.register_customer(
            SECOND["email"], SECOND["password"], SECOND["first"], SECOND["last"]
        )
        print("registered", SECOND["email"])
    except MedusaError:
        token = await client.login_customer(SECOND["email"], SECOND["password"])
        print("signed in", SECOND["email"])
    orders = (await client.get("/store/orders", params={"limit": 5}, token=token) or {}).get(
        "orders"
    ) or []
    if orders:
        print("has an order already:", orders[0]["id"])
        return str(orders[0]["id"])
    products = (
        await client.get(
            "/store/products",
            params={"handle": "ar-1001", "region_id": region, "fields": "id,*variants"},
        )
        or {}
    )["products"]
    variant = products[0]["variants"][0]["id"]
    cart = (
        await client.post(
            "/store/carts", {"region_id": region, "email": SECOND["email"]}, token=token
        )
    )["cart"]
    await client.post(
        f"/store/carts/{cart['id']}/line-items", {"variant_id": variant, "quantity": 1}, token=token
    )
    placed = await place_order(
        client, cart_id=cart["id"], email=SECOND["email"], address=ADDRESS, token=token
    )
    print("placed", placed.order_id, "for", SECOND["email"])
    return placed.order_id


async def sold_out_variant(admin: MedusaAdmin) -> None:
    products = await admin.list_all(
        "/admin/products", "products", fields="id,handle,*variants,*variants.inventory_items"
    )
    product = next(p for p in products if p["handle"] == OOS_HANDLE)
    variant = next(v for v in product["variants"] if v["title"] == OOS_VARIANT_TITLE)
    item_id = variant["inventory_items"][0]["inventory_item_id"]
    items = await admin.list_all(
        "/admin/inventory-items", "inventory_items", page=200, fields="id,*location_levels"
    )
    level = next(i for i in items if i["id"] == item_id)["location_levels"][0]
    if int(level.get("stocked_quantity") or 0) == 0:
        print("already sold out:", variant["id"])
        return
    await admin.post(
        f"/admin/inventory-items/{item_id}/location-levels/{level['location_id']}",
        {"stocked_quantity": 0},
    )
    print("set to zero:", variant["id"])


async def promo_code(admin: MedusaAdmin, client: MedusaClient) -> None:
    """A code-entry promotion (10% off every line) for the promo-code flow."""
    existing = await admin.list_all("/admin/promotions", "promotions", fields="id,code")
    if any(p.get("code") == "LAB10" for p in existing):
        print("promo code LAB10 exists")
        return
    await admin.post(
        "/admin/promotions",
        {
            "code": "LAB10",
            "type": "standard",
            "is_automatic": False,
            "status": "active",
            "application_method": {
                "type": "percentage",
                "target_type": "items",
                "allocation": "each",
                "max_quantity": 100,
                "value": 10,
                "currency_code": "usd",
            },
        },
    )
    print("created promo code LAB10")


async def mattress_disclosure(admin: MedusaAdmin) -> None:
    """A facts box on the hybrid mattress, authored on the listing's metadata."""
    products = await admin.list_all("/admin/products", "products", fields="id,handle,metadata")
    product = next(p for p in products if p["handle"] == "ar-1902")
    metadata = dict(product.get("metadata") or {})
    if metadata.get("disc:rows"):
        print("mattress disclosure exists")
        return
    metadata.update(
        {
            "disc:title": "Trial, returns and delivery",
            "disc:rows": json.dumps(
                [
                    {"label": "Home trial", "value": "100 nights", "note": "from delivery"},
                    {"label": "Return pickup fee", "value": "USD 49", "note": "waived on exchange"},
                    {"label": "Delivery", "value": "Compressed, expands within 48 hours"},
                ]
            ),
            "disc:sources": json.dumps(["Returns & refunds policy", "Product page"]),
        }
    )
    await admin.post(f"/admin/products/{product['id']}", {"metadata": metadata})
    print("mattress disclosure written")


async def pending_order_for_customer(
    client: MedusaClient, region: str, settings: LabSettings
) -> None:
    """The live merchant statements approve a cancellation each run (HS-M-11), which
    consumes one of the customer's processing orders; keep one available."""
    token = await client.login_customer(settings.lab_customer_email, settings.lab_customer_password)
    orders = (
        await client.get(
            "/store/orders",
            params={"limit": 20, "fields": "id,status,fulfillment_status"},
            token=token,
        )
        or {}
    ).get("orders") or []
    pending = [
        o
        for o in orders
        if str(o.get("status")) not in {"canceled", "cancelled", "completed"}
        and str(o.get("fulfillment_status", "not_fulfilled")) == "not_fulfilled"
    ]
    if pending:
        print(f"{settings.customer_id} has {len(pending)} processing order(s)")
        return
    products = (
        await client.get(
            "/store/products",
            params={"handle": "ar-1001", "region_id": region, "fields": "id,*variants"},
        )
        or {}
    )["products"]
    cart = (
        await client.post(
            "/store/carts", {"region_id": region, "email": settings.lab_customer_email}, token=token
        )
    )["cart"]
    await client.post(
        f"/store/carts/{cart['id']}/line-items",
        {"variant_id": products[0]["variants"][0]["id"], "quantity": 1},
        token=token,
    )
    placed = await place_order(
        client,
        cart_id=cart["id"],
        email=settings.lab_customer_email,
        address=ShippingAddress("Priya", "Lab", "1 Main St", "Austin", "78701", "US", "tx"),
        token=token,
    )
    print(f"placed a processing order for {settings.customer_id}:", placed.order_id)


async def main() -> int:
    settings = LabSettings.load()
    client = MedusaClient(settings.medusa_url, settings.medusa_publishable_key)
    region = await client.first_region_id(settings.lab_currency)
    await second_customer_order(client, region)
    admin = MedusaAdmin(
        client, email=settings.medusa_admin_email, password=settings.medusa_admin_password
    )
    await sold_out_variant(admin)
    await promo_code(admin, client)
    await mattress_disclosure(admin)
    await pending_order_for_customer(client, region, settings)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
