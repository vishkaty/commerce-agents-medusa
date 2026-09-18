"""Pure functions from Medusa Store API records to the shopping agent's types. A Medusa
product with options is a family whose variants are the purchasable records; a product
with one variant and no options is served as a plain product under the product id."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from shopping_agent import (
    Cart,
    CartItem,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ProductDetails,
)

# The fields the store API must be asked for; the default response omits most of them.
PRODUCT_FIELDS = ",".join(
    [
        "id",
        "title",
        "handle",
        "description",
        "subtitle",
        "thumbnail",
        "metadata",
        "*categories",
        "*collection",
        "*tags",
        "*type",
        "*options",
        "*options.values",
        "*variants",
        "*variants.options",
        "*variants.options.option",
        "*variants.calculated_price",
        "+variants.inventory_quantity",
        "+variants.manage_inventory",
        "+variants.allow_backorder",
    ]
)

SHORT_DESCRIPTION_CHARS = 160


def _amount(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def variant_price(variant: dict[str, Any]) -> float | None:
    calculated = variant.get("calculated_price") or {}
    return _amount(calculated.get("calculated_amount"))


def variant_currency(variant: dict[str, Any]) -> str | None:
    calculated = variant.get("calculated_price") or {}
    code = calculated.get("currency_code")
    return str(code).upper() if code else None


def variant_in_stock(variant: dict[str, Any]) -> bool:
    if not variant.get("manage_inventory", True):
        return True
    if variant.get("allow_backorder"):
        return True
    quantity = variant.get("inventory_quantity")
    return quantity is None or int(quantity) > 0


def option_values_of(variant: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in variant.get("options") or []:
        option = entry.get("option") or {}
        name = option.get("title") or entry.get("option_id")
        if name and entry.get("value") is not None:
            values[str(name)] = str(entry["value"])
    return values


def option_values_from_title(
    options: dict[str, list[str]], variant_title: str | None
) -> dict[str, str]:
    """Medusa's default variant title joins option values with " / " in option order;
    cart and order lines carry that title but not the option names."""
    if not variant_title or not options:
        return {}
    parts = [part.strip() for part in variant_title.split(" / ")]
    names = list(options)
    if len(parts) != len(names):
        return {}
    return dict(zip(names, parts, strict=True))


DEFAULT_OPTION = "Default"  # the one option a plain product carries in Medusa

# Metadata keys the importer writes that are not customer-facing attributes.
_METADATA_RESERVED = {"external_id", "brand", "rating", "review_count", "labels"}
_ATTRIBUTE_PREFIX = "attr:"
_MERCHANT_PREFIX = "mer:"  # back-office facts; never customer-facing


def product_options(product: dict[str, Any]) -> dict[str, list[str]]:
    """Option names to values; a lone ``Default`` option (Medusa needs at least one)
    means the product is plain and has no options at all."""
    options: dict[str, list[str]] = {}
    for option in product.get("options") or []:
        values = [str(v["value"]) for v in option.get("values") or [] if v.get("value") is not None]
        if option.get("title"):
            options[str(option["title"])] = values
    if list(options) == [DEFAULT_OPTION]:
        return {}
    return options


def _category(product: dict[str, Any]) -> str | None:
    categories = product.get("categories") or []
    return str(categories[0]["name"]) if categories and categories[0].get("name") else None


def _brand(product: dict[str, Any]) -> str | None:
    metadata = product.get("metadata") or {}
    if metadata.get("brand"):
        return str(metadata["brand"])
    collection = product.get("collection") or {}
    if collection.get("title"):
        return str(collection["title"])
    kind = product.get("type") or {}
    return str(kind["value"]) if kind.get("value") else None


def _labels(product: dict[str, Any]) -> list[str]:
    labels = [str(t["value"]) for t in product.get("tags") or [] if t.get("value")]
    metadata = product.get("metadata") or {}
    if metadata.get("labels"):
        labels += [part.strip() for part in str(metadata["labels"]).split(",") if part.strip()]
    return labels


def _attributes(product: dict[str, Any]) -> dict[str, str]:
    metadata = product.get("metadata") or {}
    attributes: dict[str, str] = {}
    for key, value in metadata.items():
        if value is None or key in _METADATA_RESERVED or key.startswith(_MERCHANT_PREFIX):
            continue
        name = key[len(_ATTRIBUTE_PREFIX) :] if key.startswith(_ATTRIBUTE_PREFIX) else key
        attributes[str(name)] = str(value)
    return attributes


def _rating(product: dict[str, Any]) -> tuple[float | None, int | None]:
    metadata = product.get("metadata") or {}
    rating = _amount(metadata.get("rating"))
    try:
        count = int(metadata["review_count"]) if metadata.get("review_count") else None
    except (TypeError, ValueError):
        count = None
    return rating, count


def _short(text: str | None) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= SHORT_DESCRIPTION_CHARS:
        return text
    return text[: SHORT_DESCRIPTION_CHARS - 1].rsplit(" ", 1)[0] + "…"


def variant_from_medusa(variant: dict[str, Any], product: dict[str, Any]) -> Product:
    """One purchasable record inside its family."""
    option_values = option_values_of(variant) or option_values_from_title(
        product_options(product), variant.get("title")
    )
    if list(option_values) == [DEFAULT_OPTION]:
        option_values = {}
    suffix = ", ".join(option_values.values()) or (variant.get("title") or "")
    if suffix == DEFAULT_OPTION:
        suffix = ""
    title = f"{product['title']} ({suffix})" if suffix else str(product["title"])
    attributes = _attributes(product)
    if variant.get("sku"):
        attributes["sku"] = str(variant["sku"])
    rating, review_count = _rating(product)
    return Product(
        product_id=str(variant["id"]),
        title=title,
        brand=_brand(product),
        price=variant_price(variant) or 0.0,
        currency=variant_currency(variant) or "USD",
        rating=rating,
        review_count=review_count,
        image_url=variant.get("thumbnail") or product.get("thumbnail"),
        category=_category(product),
        labels=_labels(product),
        attributes=attributes,
        in_stock=variant_in_stock(variant),
        short_description=_short(product.get("subtitle") or product.get("description")),
        option_values=option_values,
        variant_of=str(product["id"]),
    )


def product_from_medusa(product: dict[str, Any]) -> ProductDetails:
    """The family (or plain product) record with its variants."""
    raw_variants = product.get("variants") or []
    options = product_options(product)
    plain = len(raw_variants) == 1 and not options
    variants = [] if plain else [variant_from_medusa(v, product) for v in raw_variants]
    priced = [v for v in raw_variants if variant_price(v) is not None]
    in_stock_prices = [variant_price(v) for v in priced if variant_in_stock(v)]
    all_prices = [variant_price(v) for v in priced]
    price = min(in_stock_prices or all_prices or [0.0])  # type: ignore[type-var]
    currency = next((variant_currency(v) for v in raw_variants if variant_currency(v)), None)
    attributes = _attributes(product)
    if plain and raw_variants[0].get("sku"):
        attributes["sku"] = str(raw_variants[0]["sku"])
    rating, review_count = _rating(product)
    return ProductDetails(
        product_id=str(product["id"]),
        title=str(product["title"]),
        brand=_brand(product),
        price=float(price or 0.0),
        currency=currency or "USD",
        rating=rating,
        review_count=review_count,
        image_url=product.get("thumbnail"),
        category=_category(product),
        labels=_labels(product),
        attributes=attributes,
        in_stock=any(variant_in_stock(v) for v in raw_variants) if raw_variants else False,
        short_description=_short(product.get("subtitle") or product.get("description")),
        long_description=product.get("description"),
        options=options,
        variants=variants,
    )


def plain_variant_id(product: dict[str, Any]) -> str | None:
    """The single variant a plain product is bought as, else None."""
    raw_variants = product.get("variants") or []
    if len(raw_variants) == 1 and not product_options(product):
        return str(raw_variants[0]["id"])
    return None


def _line_option_values(
    line: dict[str, Any], resolved: dict[str, dict[str, str]] | None = None
) -> dict[str, str]:
    """A line's option names and values. Medusa's cart and order lines carry the variant
    title only (and the store API caps relation depth at three, so ``variant.options``
    arrives without option titles); ``resolved`` maps variant id to values read from the
    family record and wins when present."""
    variant_id = str(line.get("variant_id") or "")
    if resolved and variant_id in resolved:
        return dict(resolved[variant_id])
    variant = line.get("variant") or {}
    values = option_values_of(variant)
    if values:
        return values
    raw = line.get("variant_option_values")
    if isinstance(raw, dict) and raw:
        return {str(k): str(v) for k, v in raw.items()}
    product = line.get("product") or {}
    return option_values_from_title(product_options(product), line.get("variant_title"))


def _line_title(line: dict[str, Any]) -> str:
    product_title = line.get("product_title") or line.get("title") or ""
    variant_title = line.get("variant_title")
    if variant_title and variant_title not in {product_title, DEFAULT_OPTION}:
        return f"{product_title} ({variant_title})"
    return str(product_title)


def _is_plain_line(line: dict[str, Any], option_values: dict[str, str]) -> bool:
    """A line for a plain product: Medusa sells it through its one ``Default`` variant,
    but the shopper added the product id, so the cart hands that id back."""
    return not option_values and (line.get("variant_title") or DEFAULT_OPTION) == DEFAULT_OPTION


def _line_price(line: dict[str, Any]) -> float:
    """The unit price after the line's adjustments (promotions); Medusa states an
    adjustment as an amount off the whole line."""
    unit = _amount(line.get("unit_price")) or 0.0
    quantity = max(int(line.get("quantity") or 1), 1)
    off = sum(float(a.get("amount") or 0) for a in line.get("adjustments") or [])
    return round(max(unit - off / quantity, 0.0), 2)


def _cart_line(line: dict[str, Any], option_values: dict[str, dict[str, str]] | None) -> CartItem:
    values = _line_option_values(line, option_values)
    plain = _is_plain_line(line, values) and line.get("product_id")
    return CartItem(
        product_id=str(line["product_id"] if plain else line["variant_id"]),
        title=_line_title(line),
        price=_line_price(line),
        quantity=int(line.get("quantity") or 1),
        image_url=line.get("thumbnail"),
        option_values=values,
        variant_of=None if plain else (str(line["product_id"]) if line.get("product_id") else None),
    )


def cart_from_medusa(
    cart: dict[str, Any], option_values: dict[str, dict[str, str]] | None = None
) -> Cart:
    items = [
        _cart_line(line, option_values)
        for line in cart.get("items") or []
        if line.get("variant_id")
    ]
    return Cart(items=items, currency=str(cart.get("currency_code") or "usd").upper())


def order_status(order: dict[str, Any]) -> OrderStatus:
    status = (order.get("status") or "").lower()
    payment = (order.get("payment_status") or "").lower()
    fulfillment = (order.get("fulfillment_status") or "").lower()
    if status in {"canceled", "cancelled"}:
        return OrderStatus.CANCELLED
    if payment in {"refunded", "partially_refunded"}:
        return OrderStatus.REFUNDED
    if fulfillment in {"delivered", "partially_delivered"} or status == "completed":
        return OrderStatus.DELIVERED
    if fulfillment in {"shipped", "partially_shipped"}:
        return OrderStatus.SHIPPED
    if "return" in fulfillment:
        return OrderStatus.RETURN_INITIATED
    return OrderStatus.PROCESSING


def _placed_at(value: Any) -> datetime:
    if not value:
        return datetime.now(UTC)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def order_from_medusa(
    order: dict[str, Any], option_values: dict[str, dict[str, str]] | None = None
) -> Order:
    items = [
        OrderItem(
            product_id=str(line["variant_id"]),
            title=_line_title(line),
            quantity=int(line.get("quantity") or 1),
            price=_amount(line.get("unit_price")) or 0.0,
            option_values=_line_option_values(line, option_values),
            variant_of=str(line["product_id"]) if line.get("product_id") else None,
        )
        for line in order.get("items") or []
        if line.get("variant_id")
    ]
    return Order(
        order_id=str(order["id"]),
        status=order_status(order),
        placed_at=_placed_at(order.get("created_at")),
        items=items,
        total=_amount(order.get("total")) or 0.0,
        currency=str(order.get("currency_code") or "usd").upper(),
    )
