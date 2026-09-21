"""The upstream retail fixtures become Medusa product payloads that keep the upstream
ids as SKUs and carry brand, rating, labels and attributes in metadata."""

from __future__ import annotations

import pytest

from commerce_medusa.catalog_import import (
    ATTRIBUTE_PREFIX,
    category_name,
    medusa_product_payload,
    product_metadata,
    stock_levels,
)
from commerce_medusa.medusa_mapping import product_from_medusa
from commerce_medusa.settings import LabSettings
from demo_common.storefront_fixtures import load_catalog, load_json

DATA = LabSettings.load().fixtures_dir


@pytest.fixture(scope="module")
def catalog():
    _, listings, variants = load_catalog(DATA)
    return listings, variants


@pytest.fixture(scope="module")
def inventory() -> tuple[dict[str, int], int]:
    raw = load_json(DATA, "merchant_inventory.json")
    return {row["product_id"]: int(row["stock"]) for row in raw["inventory"]}, int(
        raw["default_stock"]
    )


def test_fixture_counts(catalog):
    listings, variants = catalog
    assert len(listings) == 87
    assert sum(1 for p in listings.values() if p.variants) == 4
    assert variants


def test_category_name():
    assert category_name("home-kitchen") == "Home Kitchen"
    assert category_name("beauty-personal-care") == "Beauty Personal Care"


def test_plain_product_payload(catalog):
    listings, _ = catalog
    record = listings["AR-1001"]
    payload = medusa_product_payload(
        record, sales_channel_id="sc_1", category_id="pcat_1", shipping_profile_id="sp_1"
    )
    assert payload["handle"] == "ar-1001"
    assert payload["shipping_profile_id"] == "sp_1"
    assert payload["status"] == "published"
    assert payload["options"] == [{"title": "Default", "values": ["Default"]}]
    assert len(payload["variants"]) == 1
    variant = payload["variants"][0]
    assert variant["sku"] == "AR-1001"
    assert variant["options"] == {"Default": "Default"}
    assert variant["prices"] == [{"currency_code": "usd", "amount": 79.0}]
    assert variant["manage_inventory"] is True
    assert payload["categories"] == [{"id": "pcat_1"}]
    assert payload["sales_channels"] == [{"id": "sc_1"}]
    assert payload["metadata"]["external_id"] == "AR-1001"
    assert payload["metadata"]["brand"] == "ACME Everyday"
    assert payload["metadata"][f"{ATTRIBUTE_PREFIX}capacity"] == "12 cups"
    assert payload["metadata"]["labels"] == "bestseller"


def test_family_payload_carries_options_and_variant_prices(catalog):
    listings, _ = catalog
    record = listings["AR-1008"]  # weighted blanket, three weights
    payload = medusa_product_payload(record, sales_channel_id="sc_1", category_id=None)
    assert "categories" not in payload
    assert payload["options"] == [{"title": "weight", "values": ["12 lb", "15 lb", "20 lb"]}]
    skus = [v["sku"] for v in payload["variants"]]
    assert skus == ["AR-1008-12LB", "AR-1008-15LB", "AR-1008-20LB"]
    assert payload["variants"][2]["prices"][0]["amount"] == 69.0
    assert payload["variants"][0]["options"] == {"weight": "12 lb"}


def test_stock_levels_use_inventory_rows_then_defaults(catalog, inventory):
    listings, _ = catalog
    stock_by_id, default_stock = inventory
    assert stock_levels(listings["AR-2102"], stock_by_id, default_stock) == {"AR-2102": 3}
    plain = next(p for pid, p in listings.items() if not p.variants and pid not in stock_by_id)
    assert stock_levels(plain, stock_by_id, default_stock) == {plain.product_id: default_stock}
    family = listings["AR-1008"]
    levels = stock_levels(family, stock_by_id, default_stock)
    assert set(levels) == {"AR-1008-12LB", "AR-1008-15LB", "AR-1008-20LB"}


def test_out_of_stock_fixture_gets_zero(catalog, inventory):
    listings, variants = catalog
    stock_by_id, default_stock = inventory
    out = [p for p in variants.values() if not p.in_stock]
    assert out, "the retail fixture has an out-of-stock variant"
    family = listings[out[0].variant_of]
    levels = stock_levels(family, {}, default_stock)
    assert levels[out[0].product_id] == 0


def test_metadata_round_trips_through_the_storefront_mapping(catalog):
    listings, _ = catalog
    record = listings["AR-1001"]
    metadata = product_metadata(record)
    medusa_product = {
        "id": "prod_x",
        "title": record.title,
        "description": record.short_description,
        "metadata": metadata,
        "options": [{"title": "Default", "values": [{"value": "Default"}]}],
        "variants": [
            {
                "id": "variant_x",
                "title": "Default",
                "sku": "AR-1001",
                "manage_inventory": True,
                "inventory_quantity": 40,
                "calculated_price": {"calculated_amount": 79, "currency_code": "usd"},
                "options": [{"option": {"title": "Default"}, "value": "Default"}],
            }
        ],
    }
    mapped = product_from_medusa(medusa_product)
    assert mapped.brand == "ACME Everyday"
    assert mapped.rating == 4.5
    assert mapped.review_count == 2841
    assert mapped.labels == ["bestseller"]
    assert mapped.attributes["capacity"] == "12 cups"
    assert "external_id" not in mapped.attributes
    assert mapped.options == {}, "a Default-only option is a plain product"
    assert not mapped.has_options
