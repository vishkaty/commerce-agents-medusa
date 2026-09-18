"""Pure mapping from Medusa Store API records to the shopping agent's types, tested over
responses recorded from the live lab server (``fixtures/medusa_*.json``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shopping_agent import OrderStatus

from commerce_medusa.medusa_mapping import (
    cart_from_medusa,
    order_from_medusa,
    product_from_medusa,
    variant_from_medusa,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def products() -> list[dict]:
    return json.loads((FIXTURES / "medusa_products.json").read_text())["products"]


@pytest.fixture(scope="module")
def shorts(products) -> dict:
    return next(p for p in products if p["handle"] == "shorts")


def test_family_carries_options_and_lowest_in_stock_price(shorts):
    family = product_from_medusa(shorts)
    assert family.product_id == shorts["id"]
    assert family.title == "Medusa Shorts"
    assert {k: sorted(v) for k, v in family.options.items()} == {"Size": ["L", "M", "S", "XL"]}
    assert family.has_options
    assert family.price == 10.0
    assert family.currency == "EUR"
    assert family.in_stock
    assert family.category == "Merch"
    assert family.image_url and family.image_url.startswith("https://")
    assert family.short_description and "shorts" in family.short_description.lower()


def test_family_details_list_every_variant_with_option_values(shorts):
    details = product_from_medusa(shorts)
    assert len(details.variants) == 4
    xl = next(v for v in details.variants if v.option_values == {"Size": "XL"})
    assert xl.product_id.startswith("variant_")
    assert xl.variant_of == shorts["id"]
    assert xl.price == 10.0
    assert xl.in_stock
    assert not xl.has_options


def test_variant_alone_resolves_with_its_family(shorts):
    raw = shorts["variants"][0]
    variant = variant_from_medusa(raw, shorts)
    assert variant.product_id == raw["id"]
    assert variant.variant_of == shorts["id"]
    assert variant.title.startswith("Medusa Shorts")
    assert variant.option_values == {"Size": raw["title"]}


def test_out_of_stock_variant_is_reported(shorts):
    raw = dict(shorts["variants"][0], inventory_quantity=0)
    assert not variant_from_medusa(raw, shorts).in_stock
    backorder = dict(raw, allow_backorder=True)
    assert variant_from_medusa(backorder, shorts).in_stock
    unmanaged = dict(raw, manage_inventory=False)
    assert variant_from_medusa(unmanaged, shorts).in_stock


def test_family_is_out_of_stock_only_when_every_variant_is(shorts):
    raw = dict(shorts, variants=[dict(v, inventory_quantity=0) for v in shorts["variants"]])
    assert not product_from_medusa(raw).in_stock


def test_cart_maps_lines_to_variant_ids_and_prices():
    cart = json.loads((FIXTURES / "medusa_cart.json").read_text())["cart"]
    variant_id = cart["items"][0]["variant_id"]
    mapped = cart_from_medusa(cart, {variant_id: {"Size": "XL"}})
    assert mapped.currency == "EUR"
    assert len(mapped.items) == 1
    line = mapped.items[0]
    assert line.product_id == cart["items"][0]["variant_id"]
    assert line.variant_of == cart["items"][0]["product_id"]
    assert line.quantity == 2
    assert line.price == 10.0
    assert line.option_values == {"Size": "XL"}
    assert mapped.subtotal == 20.0


def test_cart_line_without_resolved_options_has_none():
    cart = json.loads((FIXTURES / "medusa_cart.json").read_text())["cart"]
    assert cart_from_medusa(cart).items[0].option_values == {}


def test_order_maps_status_total_and_items():
    order = json.loads((FIXTURES / "medusa_orders.json").read_text())["orders"][0]
    mapped = order_from_medusa(order)
    assert mapped.order_id == order["id"]
    assert mapped.status is OrderStatus.PROCESSING
    assert mapped.total == 20.0
    assert mapped.currency == "EUR"
    assert mapped.placed_at.year >= 2026
    assert mapped.items[0].product_id == order["items"][0]["variant_id"]
    assert mapped.items[0].variant_of == order["items"][0]["product_id"]
    assert mapped.items[0].quantity == 1


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"status": "pending", "fulfillment_status": None}, OrderStatus.PROCESSING),
        ({"status": "completed", "fulfillment_status": "delivered"}, OrderStatus.DELIVERED),
        ({"status": "pending", "fulfillment_status": "shipped"}, OrderStatus.SHIPPED),
        ({"status": "canceled", "fulfillment_status": None}, OrderStatus.CANCELLED),
        ({"status": "pending", "payment_status": "refunded"}, OrderStatus.REFUNDED),
    ],
)
def test_order_status_mapping(raw, expected):
    order = json.loads((FIXTURES / "medusa_orders.json").read_text())["orders"][0]
    assert order_from_medusa({**order, **raw}).status is expected


def test_plain_product_cart_line_names_the_product_not_its_default_variant():
    """A shopper adds a plain product by its product id; the cart must hand that same id
    back, or the executor's cart events name a variant id the model never saw
    (found by the conformance suite)."""
    cart = json.loads((FIXTURES / "medusa_cart.json").read_text())
    line = cart["cart"]["items"][0]
    line.update(
        product_id="prod_plain",
        variant_id="variant_default",
        variant_title="Default",
        product_title="Coffee Maker",
        title="Coffee Maker",
    )
    mapped = cart_from_medusa(cart["cart"])
    assert mapped.items[0].product_id == "prod_plain"
    assert mapped.items[0].variant_of is None
    assert mapped.items[0].option_values == {}
    assert mapped.items[0].title == "Coffee Maker"


def test_family_variant_cart_line_keeps_the_variant_id():
    cart = json.loads((FIXTURES / "medusa_cart.json").read_text())
    line = cart["cart"]["items"][0]
    assert line["variant_title"] == "XL"
    mapped = cart_from_medusa(cart["cart"], {line["variant_id"]: {"Size": "XL"}})
    assert mapped.items[0].product_id == line["variant_id"]
    assert mapped.items[0].variant_of == line["product_id"]
