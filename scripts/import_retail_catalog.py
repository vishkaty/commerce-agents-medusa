"""Load the upstream retail fixtures into the lab Medusa through the Admin API.

    make import-catalog                            # idempotent: skips handles that exist
    make import-catalog ARGS=--dry-run             # print what would be created
    make import-catalog ARGS=--update-metadata     # rewrite metadata on existing

Creates (once): the USD region, a United States service zone with two USD shipping
options on the seeded warehouse, the eleven categories, then every listing from
``upstream/examples/retail/data/catalog.json`` with stock from ``merchant_inventory.json``.
Upstream ids survive as SKU, handle and ``metadata.external_id``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from commerce_medusa.catalog_import import (  # noqa: E402
    category_name,
    medusa_product_payload,
    product_metadata,
    stock_levels,
)
from commerce_medusa.medusa_client import MedusaClient, MedusaError  # noqa: E402
from commerce_medusa.settings import LabSettings  # noqa: E402
from demo_common.storefront_fixtures import load_catalog, load_json  # noqa: E402

DATA = LabSettings.load().fixtures_dir
CURRENCY = "usd"
US_ZONE = "United States"
US_SHIPPING = (
    ("Standard Shipping (US)", "standard", "Ship in 3-5 business days.", 8.0),
    ("Express Shipping (US)", "express", "Ship in 1-2 business days.", 20.0),
)


class Admin:
    """Admin API calls the importer needs, on the shared client with the admin JWT."""

    def __init__(self, client: MedusaClient, token: str) -> None:
        self.client = client
        self.token = token

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self.client.get(path, params=params or None, token=self.token) or {}

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.client.post(path, body, token=self.token)

    async def login(self, email: str, password: str) -> None:
        data = await self.client.post(
            "/auth/user/emailpass", {"email": email, "password": password}
        )
        self.token = str(data["token"])


async def ensure_region(admin: Admin, dry: bool) -> str:
    regions = (await admin.get("/admin/regions")).get("regions") or []
    for region in regions:
        if region.get("currency_code") == CURRENCY:
            return str(region["id"])
    print(f"create region United States ({CURRENCY})")
    if dry:
        return "reg_dry"
    data = await admin.post(
        "/admin/regions",
        {
            "name": "United States",
            "currency_code": CURRENCY,
            "countries": ["us"],
            "payment_providers": ["pp_system_default"],
        },
    )
    return str(data["region"]["id"])


async def ensure_us_shipping(admin: Admin, dry: bool) -> None:
    locations = (
        await admin.get(
            "/admin/stock-locations",
            fields="id,name,*fulfillment_sets,*fulfillment_sets.service_zones,*fulfillment_sets.service_zones.geo_zones",
        )
    ).get("stock_locations") or []
    if not locations:
        raise SystemExit("no stock location; seed the store first")
    location = locations[0]
    fulfillment_set = (location.get("fulfillment_sets") or [None])[0]
    if fulfillment_set is None:
        raise SystemExit("the stock location has no fulfillment set")
    zone = next(
        (z for z in fulfillment_set.get("service_zones") or [] if z.get("name") == US_ZONE), None
    )
    if zone is None:
        print(f"create service zone {US_ZONE} on {fulfillment_set['name']}")
        if not dry:
            data = await admin.post(
                f"/admin/fulfillment-sets/{fulfillment_set['id']}/service-zones",
                {"name": US_ZONE, "geo_zones": [{"type": "country", "country_code": "us"}]},
            )
            zones = data.get("fulfillment_set", {}).get("service_zones") or []
            zone = next(z for z in zones if z.get("name") == US_ZONE)
    if dry and zone is None:
        return
    profiles = (await admin.get("/admin/shipping-profiles")).get("shipping_profiles") or []
    profile_id = profiles[0]["id"]
    existing = {
        o["name"]
        for o in (await admin.get("/admin/shipping-options", fields="id,name")).get(
            "shipping_options"
        )
        or []
    }
    for name, code, description, amount in US_SHIPPING:
        if name in existing:
            continue
        print(f"create shipping option {name} {CURRENCY} {amount}")
        if dry:
            continue
        await admin.post(
            "/admin/shipping-options",
            {
                "name": name,
                "service_zone_id": zone["id"],
                "shipping_profile_id": profile_id,
                "provider_id": "manual_manual",
                "price_type": "flat",
                "type": {"label": code.capitalize(), "description": description, "code": code},
                "prices": [{"currency_code": CURRENCY, "amount": amount}],
                "rules": [
                    {"attribute": "enabled_in_store", "operator": "eq", "value": "true"},
                    {"attribute": "is_return", "operator": "eq", "value": "false"},
                ],
            },
        )


async def ensure_categories(admin: Admin, slugs: set[str], dry: bool) -> dict[str, str]:
    rows = (await admin.get("/admin/product-categories", fields="id,name,handle", limit=200)).get(
        "product_categories"
    ) or []
    by_handle = {row["handle"]: str(row["id"]) for row in rows}
    for slug in sorted(slugs):
        if slug in by_handle:
            continue
        print(f"create category {category_name(slug)} ({slug})")
        if dry:
            by_handle[slug] = "pcat_dry"
            continue
        data = await admin.post(
            "/admin/product-categories",
            {"name": category_name(slug), "handle": slug, "is_active": True},
        )
        by_handle[slug] = str(data["product_category"]["id"])
    return by_handle


async def existing_handles(admin: Admin) -> dict[str, str]:
    """Handle to product id for everything the store already has."""
    handles: dict[str, str] = {}
    offset = 0
    while True:
        data = await admin.get("/admin/products", fields="id,handle", limit=100, offset=offset)
        rows = data.get("products") or []
        handles |= {row["handle"]: str(row["id"]) for row in rows}
        offset += len(rows)
        if not rows or offset >= int(data.get("count") or 0):
            return handles


async def set_stock(
    admin: Admin, product: dict[str, Any], levels: dict[str, int], location_id: str
) -> None:
    for variant in product.get("variants") or []:
        sku = variant.get("sku")
        if sku not in levels:
            continue
        for item in variant.get("inventory_items") or []:
            await admin.post(
                f"/admin/inventory-items/{item['inventory_item_id']}/location-levels",
                {"location_id": location_id, "stocked_quantity": levels[sku]},
            )


async def main_async(dry: bool, update_metadata: bool = False) -> int:
    settings = LabSettings.load()
    if not settings.medusa_admin_email or not settings.medusa_admin_password:
        raise SystemExit("MEDUSA_ADMIN_EMAIL/PASSWORD missing in .env")
    client = MedusaClient(settings.medusa_url, settings.medusa_publishable_key)
    admin = Admin(client, "")
    await admin.login(settings.medusa_admin_email, settings.medusa_admin_password)

    _, listings, _ = load_catalog(DATA)
    inventory = load_json(DATA, "merchant_inventory.json")
    stock_by_id = {row["product_id"]: int(row["stock"]) for row in inventory["inventory"]}
    rows_by_id = {row["product_id"]: row for row in inventory["inventory"]}
    default_stock = int(inventory["default_stock"])

    await ensure_region(admin, dry)
    await ensure_us_shipping(admin, dry)
    categories = await ensure_categories(
        admin, {p.category for p in listings.values() if p.category}, dry
    )
    channels = (await admin.get("/admin/sales-channels")).get("sales_channels") or []
    channel_id = str(channels[0]["id"])
    profiles = (await admin.get("/admin/shipping-profiles")).get("shipping_profiles") or []
    profile_id = str(profiles[0]["id"]) if profiles else None
    locations = (await admin.get("/admin/stock-locations")).get("stock_locations") or []
    location_id = str(locations[0]["id"])
    present = await existing_handles(admin)

    created = skipped = failed = updated = 0
    for product_id, record in listings.items():
        if product_id.lower() in present:
            if update_metadata and not dry:
                target = present[product_id.lower()]
                await admin.post(
                    f"/admin/products/{target}",
                    {
                        "metadata": product_metadata(record, rows_by_id.get(product_id)),
                        "shipping_profile_id": profile_id,
                    },
                )
                updated += 1
            else:
                skipped += 1
            continue
        payload = medusa_product_payload(
            record,
            sales_channel_id=channel_id,
            category_id=categories.get(record.category or ""),
            currency=CURRENCY,
            merchant_row=rows_by_id.get(product_id),
            shipping_profile_id=profile_id,
        )
        levels = stock_levels(record, stock_by_id, default_stock)
        if dry:
            print(
                f"create {product_id}: {record.title} "
                f"variants={len(payload['variants'])} stock={levels}"
            )
            created += 1
            continue
        try:
            data = await admin.post("/admin/products", payload)
            product = data["product"]
            full = await admin.get(
                f"/admin/products/{product['id']}", fields="id,*variants,*variants.inventory_items"
            )
            await set_stock(admin, full["product"], levels, location_id)
            created += 1
            print(f"created {product_id} -> {product['id']}")
        except MedusaError as error:
            failed += 1
            print(f"FAILED {product_id}: {error}")
    print(f"done: created={created} updated={updated} skipped={skipped} failed={failed}")
    await client.aclose()
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--update-metadata", action="store_true")
    args = parser.parse_args()
    return asyncio.run(main_async(args.dry_run, args.update_metadata))


if __name__ == "__main__":
    sys.exit(main())
