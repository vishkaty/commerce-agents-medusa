"""Turn the upstream retail fixtures (``examples/retail/data``) into Medusa Admin API
payloads. Pure functions here; ``scripts/import_retail_catalog.py`` does the HTTP.

Every upstream product keeps its id as the Medusa SKU, handle and ``metadata.external_id``;
Medusa assigns its own product and variant ids. Brand, rating, review count, labels and
attributes travel in ``metadata`` so ``medusa_mapping`` can restore them.
"""

from __future__ import annotations

from typing import Any

from shopping_agent import ProductDetails

DEFAULT_OPTION = "Default"
ATTRIBUTE_PREFIX = "attr:"
MERCHANT_PREFIX = "mer:"  # back-office facts the storefront never shows
MERCHANT_FIELDS = ("unit_cost", "sales_last_30d", "threshold", "content_quality")


def category_name(slug: str) -> str:
    """``home-kitchen`` -> ``Home Kitchen``."""
    return " ".join(part.capitalize() for part in slug.split("-"))


def merchant_metadata(row: dict[str, Any] | None) -> dict[str, Any]:
    """``mer:*`` keys from one ``merchant_inventory.json`` row (cost, pace, threshold,
    content quality, missing attributes as a comma list)."""
    if not row:
        return {}
    metadata: dict[str, Any] = {}
    for key in MERCHANT_FIELDS:
        if row.get(key) is not None:
            metadata[f"{MERCHANT_PREFIX}{key}"] = str(row[key])
    if row.get("missing_attributes"):
        metadata[f"{MERCHANT_PREFIX}missing_attributes"] = ",".join(row["missing_attributes"])
    return metadata


def product_metadata(
    record: ProductDetails, merchant_row: dict[str, Any] | None = None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"external_id": record.product_id}
    metadata |= merchant_metadata(merchant_row)
    if record.brand:
        metadata["brand"] = record.brand
    if record.rating is not None:
        metadata["rating"] = str(record.rating)
    if record.review_count is not None:
        metadata["review_count"] = str(record.review_count)
    if record.labels:
        metadata["labels"] = ",".join(record.labels)
    for key, value in record.attributes.items():
        metadata[f"{ATTRIBUTE_PREFIX}{key}"] = str(value)
    return metadata


def stock_for(
    product_id: str,
    in_stock: bool,
    stock_by_id: dict[str, int],
    default_stock: int,
) -> int:
    if product_id in stock_by_id:
        return int(stock_by_id[product_id])
    return default_stock if in_stock else 0


def _variant_payload(
    variant_id: str,
    title: str,
    options: dict[str, str],
    price: float,
    currency: str,
) -> dict[str, Any]:
    return {
        "title": title,
        "sku": variant_id,
        "options": options,
        "manage_inventory": True,
        "allow_backorder": False,
        "prices": [{"currency_code": currency, "amount": round(float(price), 2)}],
        "metadata": {"external_id": variant_id},
    }


def medusa_product_payload(
    record: ProductDetails,
    *,
    sales_channel_id: str,
    category_id: str | None,
    currency: str = "usd",
    merchant_row: dict[str, Any] | None = None,
    shipping_profile_id: str | None = None,
) -> dict[str, Any]:
    """The ``POST /admin/products`` body for one upstream listing (plain or family). The
    shipping profile is what lets a cart holding the product complete: a shipping method
    must satisfy every item's profile."""
    description = record.long_description or record.short_description or ""
    payload: dict[str, Any] = {
        "title": record.title,
        "handle": record.product_id.lower(),
        "status": "published",
        "shipping_profile_id": shipping_profile_id,
        "description": description,
        "subtitle": record.short_description if record.long_description else None,
        "metadata": product_metadata(record, merchant_row),
        "sales_channels": [{"id": sales_channel_id}],
    }
    if category_id:
        payload["categories"] = [{"id": category_id}]
    if record.variants:
        payload["options"] = [
            {"title": name, "values": list(values)} for name, values in record.options.items()
        ]
        payload["variants"] = [
            _variant_payload(
                variant.product_id,
                ", ".join(variant.option_values.values()) or variant.product_id,
                dict(variant.option_values),
                variant.price,
                currency,
            )
            for variant in record.variants
        ]
    else:
        payload["options"] = [{"title": DEFAULT_OPTION, "values": [DEFAULT_OPTION]}]
        payload["variants"] = [
            _variant_payload(
                record.product_id,
                DEFAULT_OPTION,
                {DEFAULT_OPTION: DEFAULT_OPTION},
                record.price,
                currency,
            )
        ]
    return {key: value for key, value in payload.items() if value is not None}


def stock_levels(
    record: ProductDetails, stock_by_id: dict[str, int], default_stock: int
) -> dict[str, int]:
    """Stock per SKU (upstream id) for the listing's purchasable records."""
    if record.variants:
        return {
            variant.product_id: stock_for(
                variant.product_id, variant.in_stock, stock_by_id, default_stock
            )
            for variant in record.variants
        }
    return {
        record.product_id: stock_for(record.product_id, record.in_stock, stock_by_id, default_stock)
    }
